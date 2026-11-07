"""
wsframe.py - a minimal, dependency-free RFC 6455 WebSocket server.

Just enough of the protocol to accept a browser upgrade, then exchange binary
and text frames: the opening handshake (Sec-WebSocket-Accept), frame
decode/encode with client-mask handling, fragmentation reassembly, ping/pong
and close. Used by the audio bridge (binary PCM both ways).

No third-party packages - pure stdlib. If you would rather use the pure-Python
``websockets`` library, it is listed as an OPTIONAL dependency in
requirements.txt, but this module keeps the service at zero dependencies.
"""

import base64
import hashlib
import socket
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


def handshake(conn, raw_headers):
    """Complete the opening handshake given the raw HTTP request headers.

    ``raw_headers`` is the decoded request head (request line + headers).
    Returns True on success (101 sent), raises WebSocketError otherwise.
    """
    headers = {}
    for line in raw_headers.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    key = headers.get("sec-websocket-key")
    if headers.get("upgrade", "").lower() != "websocket" or not key:
        raise WebSocketError("not a websocket upgrade request")

    accept = base64.b64encode(
        hashlib.sha1((key + _GUID).encode("ascii")).digest()
    ).decode("ascii")

    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
    )
    conn.sendall(response.encode("ascii"))
    return True


class WebSocket:
    """A connected WebSocket over a blocking socket."""

    def __init__(self, conn):
        self.conn = conn
        self._buf = b""
        self.closed = False

    # --- receiving -----------------------------------------------------------
    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        """Receive one application message. Returns (opcode, bytes) or (None, None)
        on a clean close. Handles fragmentation and answers ping/close."""
        message = b""
        msg_op = None
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))

            if opcode == OP_CLOSE:
                self.close()
                return (None, None)
            if opcode == OP_PING:
                self.send(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CONT:
                message += payload
            else:
                msg_op = opcode
                message = payload
            if fin:
                return (msg_op, message)

    # --- sending -------------------------------------------------------------
    def send(self, data, opcode=OP_BINARY):
        """Send one frame (server->client frames are never masked)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
            opcode = OP_TEXT
        b0 = 0x80 | opcode
        length = len(data)
        header = bytearray([b0])
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        try:
            self.conn.sendall(bytes(header) + data)
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise WebSocketError(str(exc))

    def send_binary(self, data):
        self.send(data, OP_BINARY)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.sendall(bytes([0x80 | OP_CLOSE, 0x00]))
        except OSError:
            pass
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass
