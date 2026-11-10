# External LTE Antenna — what to buy and how to fit it

Context: the dongle **attaches to Digi on LTE but passes no data** — the link is
signal-starved on a supported band (B1/2100; see `CAPABILITIES.md`). An external
antenna adds the link margin it's missing. It **cannot add unsupported bands**
(B20/800, B7/2600) — it boosts the bands the board already uses.

## What to buy
**Recommended: `4G LTE directional log-periodic antenna 698–2700 MHz SMA`, ~9–11 dBi.**

Why directional: your problem is weak signal, so **gain + aim** beat a wideband omni.
A log-periodic/Yagi/panel you point at the Digi tower gives the most margin.

Checklist when buying:
| Spec | Want | Why |
|------|------|-----|
| Frequency range | **698–2700 MHz** | covers B3 (1800) + B1 (2100) — your usable bands — and more |
| Type | **Directional** (log-periodic / Yagi / panel) | highest gain, aimable at the tower |
| Gain | **~9–11 dBi** | link margin for the marginal signal |
| Impedance | **50 Ω** | all real LTE antennas; never a 75 Ω TV antenna |
| Connector | **SMA** (match your pigtail) | standard; get antenna SMA-male / pigtail SMA-female (or vice-versa) |
| Mounting | outdoor-rated if mounting outside | weatherproofing |

Alternatives:
- **Cheap first test:** `wideband LTE SMA whip 700–2700 MHz` (~3–5 dBi, omni) — no aiming, lower gain. Good to confirm the idea before buying the directional.
- **MIMO:** N/A here — **this board has only ONE antenna connector**, so a single (SISO)
  antenna is the only option. Don't buy a 2× MIMO antenna expecting both ports to work.

> **This board has a single connector** (confirmed by owner) — almost certainly a
> **u.FL / IPEX (MHF)** snap socket. That means **no soldering**: you just clip a
> **u.FL→SMA pigtail** onto it (see below). Buy ONE directional antenna + ONE pigtail.

Band → frequency (so you can read any antenna's spec sheet):
`B3 = 1800 MHz`, `B1 = 2100 MHz`, `B8 = 900`, `B5 = 850`, `B40 = 2300`. A 698–2700 MHz
antenna spans all of them.

## How to aim it
1. Find the nearest **Digi** tower/direction with **CellMapper** or **OpenSignal**
   (filter to Digi / RCS&RDS, LTE).
2. Mount the antenna **high, with line of sight** toward that tower (window, wall, pole).
3. Point the **narrow/front end** at the tower (log-periodic radiates off the small end).
4. Re-run the signal test after aiming and tweak a few degrees for best RSRP:
   ```
   adb shell modem_at "AT\$QCRSRP?"              # EARFCN + RSRP per cell (want B1/EARFCN 525 less negative)
   adb shell busybox ping -c5 -I rmnet0 8.8.8.8  # should start getting replies
   ```

## Connecting it to the dongle — NO SOLDERING (you have one u.FL connector)
Because the board has a single **u.FL/IPEX (MHF)** connector, this is plug-in:
1. Buy a **u.FL/IPEX → SMA-female pigtail** (short, RG178/RG316). Match the socket type
   on the board — most are standard **u.FL**; if it's the smaller **MHF4**, get that
   variant. (Easy way to be sure: photograph the connector and compare, or measure it.)
2. Open the case, **gently clip the u.FL plug straight down** onto the board socket (they
   press-fit; don't force at an angle — the center pin is fragile).
3. Route the pigtail out of the case (strain-relieve it), and screw your **SMA** antenna /
   SMA-male antenna cable onto the pigtail's SMA end.
4. **Never power the modem with the connector open** — attach the antenna before use.

> If the single connector turns out to be a bare solder pad rather than a u.FL socket:
> solder a **50 Ω coax (RG178/RG316)** center→feed pad, braid→ground, exposed center lead
> **a few mm max**, out to SMA. But a "connector" almost always means a u.FL socket = no soldering.

## Expectation
Because it already **attaches** to Digi on B1/2100 (just can't carry data), extra margin
from a directional antenna + aiming should push it into **usable data** — provided a
Digi B1/B3 cell is within reach. If after a good directional antenna, high mount, and
correct aim it still won't pass data, the supported-band coverage genuinely isn't there
and you'd need a different location or carrier.
