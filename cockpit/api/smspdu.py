"""
smspdu.py - minimal SMS PDU encode/decode (GSM 03.40 / 23.040).

Pure-Python, stdlib only. Supports what this modem needs:
  * decode SMS-DELIVER PDUs returned by ``AT+CMGL``/``AT+CMGR`` in PDU mode,
    including GSM 7-bit default alphabet and UCS2 (unicode) bodies, the service
    centre timestamp, and concatenation UDH (so multipart SMS can be reassembled
    by the caller).
  * encode SMS-SUBMIT PDUs for ``AT+CMGS`` in PDU mode, auto-selecting GSM 7-bit
    when the text fits the default alphabet, otherwise UCS2, and splitting long
    messages into concatenated parts with a UDH.

The modem also supports text mode (``AT+CMGF=1``); modem.py uses text mode for
the common ASCII path and this PDU codec for unicode and for robust decoding.

References: 3GPP TS 23.040 (TPDU), 23.038 (alphabets/DCS).
"""

# --- GSM 7-bit default alphabet (3GPP TS 23.038) ---------------------------
# Index = 7-bit code point -> unicode char. 0x1B marks an escape to the
# extension table.
_GSM_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
_GSM_EXT = {
    0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
    0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€",
}
# Reverse maps for encoding.
_GSM_BASIC_REV = {ch: i for i, ch in enumerate(_GSM_BASIC)}
_GSM_EXT_REV = {ch: i for i, ch in _GSM_EXT.items()}


# ===========================================================================
# 7-bit alphabet helpers
# ===========================================================================
def gsm7_encodable(text):
    """True if every character can be represented in the GSM 7-bit alphabet."""
    for ch in text:
        if ch not in _GSM_BASIC_REV and ch not in _GSM_EXT_REV:
            return False
    return True


def _text_to_septets(text):
    """Map text to a list of 7-bit code points (escapes expand to two)."""
    out = []
    for ch in text:
        if ch in _GSM_BASIC_REV:
            out.append(_GSM_BASIC_REV[ch])
        elif ch in _GSM_EXT_REV:
            out.append(0x1B)
            out.append(_GSM_EXT_REV[ch])
        else:
            out.append(_GSM_BASIC_REV["?"])
    return out


def _pack_septets(septets, udh_septets=0):
    """Pack 7-bit code points into octets.

    ``udh_septets`` is the number of leading fill bits (as septets) used to
    byte-align the user data after a UDH; callers that have no UDH pass 0.
    """
    # Fill-bit alignment: shift the start so the 7-bit data begins on a septet
    # boundary after the UDH octets.
    shift = (7 - (udh_septets * 7) % 7) % 7 if udh_septets else 0
    result = bytearray()
    buffer = 0
    bits = 0
    if shift:
        buffer = 0
        bits = shift
    for code in septets:
        buffer |= (code << bits)
        bits += 7
        while bits >= 8:
            result.append(buffer & 0xFF)
            buffer >>= 8
            bits -= 8
    if bits > 0:
        result.append(buffer & 0xFF)
    return bytes(result)


def _unpack_septets(data, count, skip_bits=0):
    """Unpack ``count`` 7-bit code points from packed octets."""
    septets = []
    data = list(data)
    buffer = 0
    bits = 0
    for byte in data:
        buffer |= byte << bits
        bits += 8
        while bits >= 7 and len(septets) < count + (skip_bits // 7):
            septets.append(buffer & 0x7F)
            buffer >>= 7
            bits -= 7
    if skip_bits:
        drop = skip_bits // 7
        septets = septets[drop:]
    return septets[:count]


def _septets_to_text(septets):
    out = []
    escaped = False
    for code in septets:
        if escaped:
            out.append(_GSM_EXT.get(code, "?"))
            escaped = False
        elif code == 0x1B:
            escaped = True
        else:
            out.append(_GSM_BASIC[code] if code < len(_GSM_BASIC) else "?")
    return "".join(out)


# ===========================================================================
# Address (phone number) encoding - semi-octet / swapped nibbles
# ===========================================================================
def _encode_address(number):
    """Return (type_of_address, semi-octet digits hex) for a dialling number."""
    intl = number.startswith("+")
    digits = number.lstrip("+")
    toa = 0x91 if intl else 0x81  # international vs national/unknown
    if len(digits) % 2:
        digits += "F"
    swapped = "".join(digits[i + 1] + digits[i] for i in range(0, len(digits), 2))
    return toa, swapped


def _decode_address(toa, semi):
    """Decode swapped-nibble address digits into an MSISDN string."""
    digits = ""
    for i in range(0, len(semi), 2):
        pair = semi[i:i + 2]
        digits += pair[1] + pair[0]
    digits = digits.rstrip("F").rstrip("f")
    if toa == 0x91:  # international
        return "+" + digits
    return digits


# ===========================================================================
# Service centre timestamp
# ===========================================================================
def _decode_scts(octets):
    """Decode the 7-octet service-centre timestamp into an ISO-ish string."""
    def swap(b):
        # Each octet holds two BCD digits with swapped nibbles.
        return int(("%02x" % b)[::-1])
    try:
        yy = swap(octets[0]); mo = swap(octets[1]); dd = swap(octets[2])
        hh = swap(octets[3]); mi = swap(octets[4]); ss = swap(octets[5])
        return "20%02d-%02d-%02d %02d:%02d:%02d" % (yy, mo, dd, hh, mi, ss)
    except Exception:
        return None


# ===========================================================================
# Decode SMS-DELIVER (incoming)
# ===========================================================================
def decode_deliver(pdu_hex):
    """Decode an SMS-DELIVER PDU.

    Returns a dict:
      {sender, text, timestamp, dcs, udh (dict|None),
       concat {ref, total, seq} (optional)}
    The leading SMSC field is parsed and skipped.
    """
    data = bytes.fromhex(pdu_hex.strip())
    i = 0
    # SMSC: length octet then that many octets.
    smsc_len = data[i]; i += 1
    i += smsc_len

    first = data[i]; i += 1
    # first & 0x03 == 0 for SMS-DELIVER (MTI); we accept any for robustness.
    udhi = bool(first & 0x40)      # user-data-header present

    # Originating address.
    addr_digits = data[i]; i += 1
    toa = data[i]; i += 1
    addr_octets = (addr_digits + 1) // 2
    semi = data[i:i + addr_octets].hex().upper(); i += addr_octets
    sender = _decode_address(toa, semi[:addr_digits + (addr_digits % 2)])

    pid = data[i]; i += 1          # protocol id
    dcs = data[i]; i += 1          # data coding scheme
    scts = _decode_scts(data[i:i + 7]); i += 7

    udl = data[i]; i += 1          # user data length
    ud = data[i:]

    is_ucs2 = (dcs & 0x0C) == 0x08
    is_8bit = (dcs & 0x0C) == 0x04

    udh = None
    concat = None
    udh_len = 0
    if udhi:
        udh_len = ud[0] + 1        # total UDH length incl the length octet
        udh, concat = _parse_udh(ud[1:ud[0] + 1])

    if is_ucs2:
        body = ud[udh_len:]
        # UCS2 body length = udl - udh bytes (udl is in octets for UCS2).
        nbytes = udl - udh_len
        text = body[:nbytes].decode("utf-16-be", "replace")
    elif is_8bit:
        text = ud[udh_len:udl].decode("latin-1", "replace")
    else:
        # GSM 7-bit: udl counts septets. Fill-bit align after the UDH.
        skip_bits = 0
        if udh_len:
            udh_bits = udh_len * 8
            skip_bits = (7 - (udh_bits % 7)) % 7
        septets_total = udl
        all_septets = _unpack_septets(ud, septets_total, 0)
        if udh_len:
            # Drop septets covered by the UDH plus the fill bits.
            drop = (udh_len * 8 + skip_bits) // 7
            all_septets = all_septets[drop:]
        text = _septets_to_text(all_septets)

    result = {
        "sender": sender,
        "text": text,
        "timestamp": scts,
        "dcs": dcs,
        "pid": pid,
        "udh": udh,
    }
    if concat:
        result["concat"] = concat
    return result


def _parse_udh(udh_bytes):
    """Parse UDH information elements; return (raw_elements, concat_info)."""
    elements = []
    concat = None
    i = 0
    while i + 1 < len(udh_bytes):
        iei = udh_bytes[i]; ielen = udh_bytes[i + 1]
        val = udh_bytes[i + 2:i + 2 + ielen]
        elements.append({"iei": iei, "data": val.hex()})
        if iei == 0x00 and ielen == 3:       # concat, 8-bit ref
            concat = {"ref": val[0], "total": val[1], "seq": val[2]}
        elif iei == 0x08 and ielen == 4:     # concat, 16-bit ref
            concat = {"ref": (val[0] << 8) | val[1], "total": val[2], "seq": val[3]}
        i += 2 + ielen
    return elements, concat


# ===========================================================================
# Encode SMS-SUBMIT (outgoing)
# ===========================================================================
def encode_submit(number, text, reference=0):
    """Encode one or more SMS-SUBMIT PDUs for AT+CMGS.

    Returns a list of (tpdu_length, pdu_hex). ``tpdu_length`` is the value that
    precedes the PDU on the ``AT+CMGS=<len>`` line (octets of the PDU excluding
    the SMSC field). Long messages are split into concatenated parts.
    """
    use_ucs2 = not gsm7_encodable(text)
    if use_ucs2:
        parts = _split_ucs2(text)
        dcs = 0x08
    else:
        parts = _split_gsm7(text)
        dcs = 0x00

    multipart = len(parts) > 1
    pdus = []
    for seq, part in enumerate(parts, start=1):
        pdu = _build_submit(number, part, dcs, use_ucs2,
                            concat=(reference, len(parts), seq) if multipart else None)
        pdus.append(pdu)
    return pdus


def _split_gsm7(text):
    # 160 septets single, 153 per part when concatenated (UDH eats 7 septets).
    septets = _text_to_septets(text)
    if len(septets) <= 160:
        return [text]
    # Re-split on character boundaries approximately; rebuild per-part text.
    parts = []
    cur = []
    cur_len = 0
    for ch in text:
        clen = 2 if ch in _GSM_EXT_REV else 1
        if cur_len + clen > 153:
            parts.append("".join(cur))
            cur = [ch]; cur_len = clen
        else:
            cur.append(ch); cur_len += clen
    if cur:
        parts.append("".join(cur))
    return parts


def _split_ucs2(text):
    # 70 UCS2 chars single, 67 per concatenated part.
    if len(text) <= 70:
        return [text]
    size = 67
    return [text[i:i + size] for i in range(0, len(text), size)]


def _build_submit(number, text, dcs, use_ucs2, concat=None):
    toa, semi = _encode_address(number)
    digits = number.lstrip("+")
    addr_len = len(digits)

    first = 0x01  # SMS-SUBMIT, VPF=0
    if concat:
        first |= 0x40  # UDHI

    # Build user data.
    udh = b""
    if concat:
        ref, total, seq = concat
        udh = bytes([0x05, 0x00, 0x03, ref & 0xFF, total, seq])

    if use_ucs2:
        body = text.encode("utf-16-be")
        ud = (bytes([len(udh)]) if concat else b"") + udh + body
        udl = len(udh) + (1 if concat else 0) + len(body)
        # For 8-bit/UCS2, UDL is octet count of the whole UD field.
        udl = len(ud)
    else:
        septets = _text_to_septets(text)
        if concat:
            udh_full = bytes([len(udh)]) + udh
            udh_bits = len(udh_full) * 8
            fill = (7 - (udh_bits % 7)) % 7
            packed = _pack_septets(septets, udh_septets=0)
            # Prepend UDH, then pack text with fill-bit alignment.
            packed = _pack_with_udh(udh_full, septets)
            udl = (len(udh_full) * 8 + fill) // 7 + len(septets)
            ud = packed
        else:
            packed = _pack_septets(septets)
            ud = packed
            udl = len(septets)

    tp = bytearray()
    tp.append(first)
    tp.append(0x00)              # TP-MR message reference (let modem fill / 0)
    tp.append(addr_len)
    tp.append(toa)
    tp += bytes.fromhex(semi)
    tp.append(0x00)             # TP-PID
    tp.append(dcs)              # TP-DCS
    tp.append(udl)
    tp += ud

    # SMSC length 0 => use the modem's stored SMSC.
    pdu = "00" + tp.hex().upper()
    tpdu_len = len(tp)          # octets excluding the SMSC byte
    return tpdu_len, pdu


def _pack_with_udh(udh_full, septets):
    """Pack a UDH (octets) followed by GSM7 septets with fill-bit alignment."""
    udh_bits = len(udh_full) * 8
    fill = (7 - (udh_bits % 7)) % 7
    out = bytearray(udh_full)
    buffer = 0
    bits = fill  # leading fill bits so text starts on a septet boundary
    # The fill bits live in the last UDH octet's high bits implicitly; standard
    # practice is to start the packing accumulator pre-loaded with `fill` bits.
    for code in septets:
        buffer |= (code << bits)
        bits += 7
        while bits >= 8:
            out.append(buffer & 0xFF)
            buffer >>= 8
            bits -= 8
    if bits > 0:
        out.append(buffer & 0xFF)
    return bytes(out)
