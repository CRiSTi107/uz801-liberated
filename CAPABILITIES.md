# UZ801 Band Capabilities → Which Carriers Work

What LTE bands this dongle supports, which carriers that maps to (Romania first,
then global), and what the on-site band scan actually found.

## Board LTE band support
| Band | Freq (MHz) | Duplex | Supported? |
|------|-----------|--------|:---------:|
| B1 | 2100 | FDD | ✅ |
| B3 | 1800 | FDD | ✅ |
| B5 | 850 | FDD | ✅ |
| B8 | 900 | FDD | ✅ |
| B40 | 2300 | TDD | ✅ |
| B7 | 2600 | FDD | ❌ |
| B20 | 800 | FDD | ❌ |
| B28 | 700 | FDD | ❌ |
| B38 / B41 | 2600 / 2500 | TDD | ❌ |
| B2/B4/B12/B13/B66/B71 (North America) | — | — | ❌ |

**One-line summary:** great on **B3 (1800)** and **B1 (2100)** — the two dominant
European/global LTE bands — plus B8/B5 and TDD **B40** (India/APAC). **Blind to
B20 (800)** and **B7 (2600)**, which are Europe's wide-area-coverage and
dense-capacity layers. Essentially **incompatible with North America**.

## On-site band scan (this location, Digi RO SIM)
`$QCRSRP` measured cells:
| EARFCN | Band | RSRP | Board can use? |
|-------:|------|-----:|:--------------:|
| 525 | **B1 (2100)** | **−126.5 dBm** (weak) | ✅ yes |
| 6425 | B20 (800) | −44 dBm (very strong) | ❌ no |
| 3650 | B8 (900) | none | ✅ (but nothing here) |

**Key takeaway:** the strong signal (−44) is **B20**, which the board can't use —
*that's* why it looks "dead." But there **is** a usable-band signal: **B1/2100 at
−126.5 dBm**. That's weak (near the attach threshold) but real. So this is a
**signal-strength problem on a supported band**, not a hard incompatibility — an
external antenna / better positioning aimed at the 2100 cell, or moving to stronger
B1/B3 coverage, can make it attach.

## Romania — carrier × band (approximate current deployments)
| Carrier | LTE bands (RO) | Works on this board via |
|---------|----------------|-------------------------|
| **Digi (RCS&RDS)** | **B1 (2100)**, **B3 (1800)** | ✅ B1 **and** B3 — good match |
| **Orange RO** | B3 (1800), B1 (2100), B7 (2600), B20 (800) | ✅ B3, B1 (not B7/B20) |
| **Vodafone RO** | B3 (1800), B1 (2100), B7 (2600), B20 (800) | ✅ B3, B1 (not B7/B20) |
| **Telekom RO** | B3 (1800), B20 (800), B7 (2600) | ✅ B3 (not B7/B20) |

Notes:
- **All four** Romanian carriers run **B3 (1800)** as their urban/suburban workhorse →
  in any town/city with B3 coverage, **any** of them works on this dongle.
- The board's only real gap in RO is **B20 (800)** — the rural / deep-indoor coverage
  layer (Orange/Vodafone/Telekom). The strong B20 cell you see is almost certainly
  **one of those three**, not Digi (Digi historically has little/no 800 MHz).
- **Your Digi SIM is a fine match for the board** — Digi uses B1 (2100) + B3 (1800),
  both supported. The weak **B1 @ −126.5 dBm** you're seeing is consistent with a
  Digi 2100 cell. You're **signal-starved on a supported band**, not incompatible.

### Best bet for you, in order
1. **Stay on Digi**, but improve the **B1 (2100)** signal: external 2100-capable LTE
   antenna + reposition near a window / higher up / toward the cell.
2. Or use the dongle where **Digi B3 (1800)** is present (denser urban) — strongest
   match.
3. Any RO carrier works in **B3 (1800)** coverage; pick by local B3/B1 strength, not
   by the (unusable) strong B20.

## United Kingdom — carrier × band (approximate)
| Carrier | LTE bands (UK) | Works on this board via |
|---------|----------------|-------------------------|
| **EE** | B3 (1800), B1 (2100), B7 (2600), B20 (800) | ✅ B3, B1 |
| **Three** | B3 (1800), B1 (2100), B20 (800) | ✅ B3, B1 |
| **O2** | B3 (1800), **B40 (2300 TDD)**, B20 (800) | ✅ B3 **and** B40 |
| **Vodafone** | B3 (1800), B1 (2100), B7 (2600), B20 (800), B32 (1500 SDL) | ✅ B3, B1 |

- **All four** use **B3 (1800)** → any of them works in towns/cities.
- **O2** also uses **B40 (2300 TDD)**, which this board supports — a bonus match.
- Gap = **B20 (800)** (rural / deep-indoor) and **B7 (2600)** (urban capacity): weak in
  the countryside, fine in built-up areas. Best picks: **EE** / **Three** (heavy 1800),
  or **O2** (1800 + 2300).

## Global (if you travel / reSIM)
| Region | Fit | Why |
|--------|-----|-----|
| **Europe** | ✅ Excellent | B3 (1800) + B1 (2100) ubiquitous; B8 (900) refarm. (Loses B20/B7 coverage/capacity layers.) |
| **India** | ✅ Excellent | Jio = B40 (2300 TDD) + B3 + B5; Airtel/Vi = B3/B1/B40 — all supported. |
| **SE Asia / APAC** | ✅ Good | B40 TDD, B3, B1 common. |
| **Africa / LATAM** | ✅ Good where B3/B1 | B3 (1800) widely used. |
| **North America** | ❌ Poor/unusable | US/Canada LTE = B2/4/5/12/13/66/71; only B5 (850) partial overlap → won't work on AT&T/Verizon/T-Mobile. |

## How to confirm which carrier owns a cell
A full `AT+COPS=?` operator scan lists PLMNs per band, but it needs enough signal to
complete — it returned empty here because B1 is marginal and B20 isn't usable. Re-run
it once the B1/2100 signal is stronger (antenna/position):
```
adb shell modem_at "AT+COPS=?"     # lists reachable operators on supported bands
adb shell modem_at "AT\$QCRSRP?"   # per-cell EARFCN + RSRP (band presence/strength)
```

---
*Band deployments are approximate (knowledge cutoff) and change over time; verify
locally with the scan. EARFCN→band: 525=B1, 6425=B20, 3650=B8, 1800≈B3, 3100≈B40.*
