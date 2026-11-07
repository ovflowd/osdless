# Protocol notes

What osdless knows about ASUS monitors over DDC/CI, and where each fact comes from. It covers the
codes and values osdless reads or writes, the replies it has to watch out for, and how it tells
monitors apart. It is not a full map of every ASUS code.

Every fact carries one of these labels:

| Label | Meaning |
|---|---|
| **VERIFIED** | Read from a real PG39WCDM (firmware 103, VCP table 2.7) with ddcutil 3.0.2 over DisplayPort, or confirmed in its OSD |
| **INFERRED** | From the MCCS standard, the ddcutil documentation, the Linux kernel source, or the codes, values and rules ASUS's Windows app (DisplayWidget Center 1.5) uses. That app was studied only to learn those, for interoperability; none of its code or files are in this repository |
| **THIRD-PARTY** | Seen by another project on another model, linked. Not checked with osdless |

The lists of codes osdless never sends or never writes are `NEVER_SEND` and `NEVER_WRITE` at the
top of [osdless.py](../osdless.py), and the [safety model](how-it-works.md#safety-model) gives
the reason for each. This page does not repeat them.

## DDC/CI in one paragraph

DDC/CI is an I2C channel in the display cable, with the monitor at address `0x37`. MCCS, the VESA
standard on top of it, names each setting by a one-byte VCP code. A read returns four bytes: the
maximum (`mh`, `ml`) and the current value (`sh`, `sl`), so both are 16 bits: `cur = sh << 8 | sl`,
`max = mh << 8 | ml`. `ddcutil getvcp --verbose` prints them as
`Raw value: opcode=0xfd, mh=0x00, ml=0xfd, sh=0x00, sl=0x29` (VERIFIED). A write sends a 16-bit
value; `ddcutil setvcp` takes `0x00` to `0xffff` (INFERRED, ddcutil 3.0.2 man page). Codes `0xE0` to
`0xFF` are left to the manufacturer (INFERRED, MCCS). The monitor wants at least 50 ms between
commands (INFERRED, DDC/CI), and ddcutil spaces its own calls only within one process.

## Which I2C bus

The bus layout depends on the GPU driver. osdless writes over a DisplayPort connector's own DP-AUX
adapter, or over the `ddc` adapter of an HDMI or DVI connector, and only reads anywhere else.

| Connection | Bus ddcutil uses | Evidence |
|---|---|---|
| DisplayPort on amdgpu | The connector's own `i2c-N` directory under `/sys/class/drm/cardX-DP-Y/`, the DP-AUX channel. The connector's `ddc` symlink points at another adapter, which ddcutil does not use for the monitor | VERIFIED |
| DisplayPort on i915 and xe | The same DP-AUX directory, and the `ddc` symlink points at it too | INFERRED from the kernel source (`drm_connector_init_with_ddc` with the DP-AUX adapter); not run on Intel graphics |
| A passive DisplayPort-to-HDMI adapter on amdgpu | The DisplayPort connector's separate `ddc` adapter. osdless only reads a monitor there | INFERRED |
| HDMI on amdgpu | The connector only has a `ddc` symlink | VERIFIED in sysfs; DDC/CI over HDMI never tried |
| NVIDIA's driver, MST hubs, docks | May not map to a DRM connector at all; amdgpu, i915 and nouveau give an MST connector no `ddc` adapter | INFERRED, untested |
| An active adapter or a dock with an HDMI port | A DisplayPort connector, while the monitor is on HDMI. osdless reads `0x60` before a write over DisplayPort to catch this | INFERRED |

## Capabilities

The capabilities string lists each supported code, with its allowed values in brackets. Two ASUS
codes break the pattern: `FC(HHLL)` and `FD(HHLL)` hold one 16-bit mask of the supported bits, not a
list of values (VERIFIED: the PG39WCDM lists `FC(049F)` and `FD(00FD)`, and ddcutil complains about
both). A code in the list only says the monitor answers it; what it means depends on the VCP table
and the product line (below).

The model in the string is not always the EDID model: the published ProArt strings say
`model(ProArtDisplay)` (INFERRED, public strings). osdless writes only when the two agree.

## Telling ASUS monitors apart

| Step | Rule | Evidence |
|---|---|---|
| Maker | The EDID maker is `AUS` | VERIFIED |
| Support code | `0xEF`'s current value is `0x1A`, or in one of `0x55`–`0x58`, `0x66`–`0x69`, `0x77`–`0x7A`, `0x88`–`0x8B`, `0x99`–`0x9C`, `0xA0`–`0xA3`, `0xB1`–`0xB4`, `0xC2`–`0xC5`, `0xD3`–`0xD6`, `0xE4`–`0xE7`, `0xF5`–`0xF8`. The PG39WCDM reads `0xB1` | INFERRED; VERIFIED for the PG39WCDM |
| VCP table | `0xEF`'s maximum: high byte = table version, low byte = revision. The PG39WCDM reads `0x0207`, table 2.7 | INFERRED; VERIFIED for the PG39WCDM |
| Product line | From the model name: `PG`, `XG`, `VG` gaming (written); anything else read-only | INFERRED |
| OLED | A gaming model whose 7th character is `D` or `W`. Only then does osdless offer the `oled` commands | INFERRED, ASUS's app uses this rule; all five OLEDs in the fixtures fit it |

Codes that move between tables (INFERRED):

| Setting | Table 2 | Older tables |
|---|---|---|
| Power saving | `0xE1` | `0xEE` |
| HDR mode | `0xE2` | `0xF8` |
| ASCR | `0xFD` bit `0x0002` | `0xE1` |
| Screen move | `0xF9` | not available |

So on table 2, `0xF8` is the pixel cleaning reminder and `0xEE` is ELMB, while on older tables
`0xF8` switches HDR. osdless writes `0xE1`, `0xF8` and `0xF9` only on table 2, and only reads a
monitor on any other table, a future table 3 included.

## Codes osdless decodes

On ASUS gaming monitors with table 2 (the `asus-v2-gaming` profile):

| Code | Setting | Values | Evidence |
|---|---|---|---|
| `0x10` | Brightness | 0 to max | INFERRED (MCCS); never written |
| `0x12` | Contrast | 0 to max | INFERRED (MCCS) |
| `0x14` | Colour preset | `01` sRGB, `05` 6500 K, `08` 9300 K, `0B` User 1, … | INFERRED (MCCS); the PG39WCDM reads `05`, 6500 K |
| `0x60` | Input | `0F` DisplayPort, `11` HDMI-1, `12` HDMI-2, `1A` USB-C | INFERRED; `1A` is ASUS's own value above the MCCS range. THIRD-PARTY (display-toolkit): unreliable while auto input detection is on, so a value osdless cannot place only makes a write untested |
| `0x62` | Volume | 0 to max | INFERRED (MCCS) |
| `0x72` | Gamma | Low byte: `50` 1.8, `64` 2.0, `78` 2.2, `8C` 2.4, `A0` 2.6 | VERIFIED: the PG39WCDM lists `72(50 64 78 8C A0)` and reads `0x0078`. THIRD-PARTY: some models put it in the high byte (`72(5000 …)`, reading `0x7800`), which osdless shows as unknown |
| `0xD6` | Power mode | `01` on, `05` off | INFERRED (MCCS); never written |
| `0xDC` | GameVisual | `01` Cinema, `02` Scenery, `03` sRGB, `04` User, `05` Racing, `06` RTS, `07` FPS, `08` MOBA, `09` Night Vision, `0A` sRGB Cal | INFERRED; these are ASUS names, not the MCCS ones |
| `0xE1` | Power Setting | `00` Standard, `01` Power Saving | VERIFIED in the PG39WCDM's OSD |
| `0xE2` | HDR mode | `00` off; `01` to `03` Gaming, Cinema and Console HDR, in an inferred order | INFERRED. VERIFIED: the PG39WCDM reads `0`, which its list `E2(01 02 03)` does not contain. Never written |
| `0xF3` | KVM | `01` Smart KVM, `02` USB-B upstream, `03` USB-C upstream, `04` Auto KVM | INFERRED; models list different subsets |
| `0xF8` | Pixel cleaning reminder | `00` off, `02`, `04`, `08` hours | INFERRED |
| `0xF9` | Screen move | `00` off, `01` light, `02` middle, `03` strong | INFERRED |
| `0xFB` | OSD shortcuts | `sl` = shortcut 1, `sh` = shortcut 2: `01` GamePlus, `02` GameVisual, … `0C` KVM, `0F` Pixel Cleaning | INFERRED; the PG39WCDM reads `0x0102`, GameVisual and GamePlus |
| `0xFC` | Toggle word 1 | Bits, below | Read-only |
| `0xFD` | Toggle word 2 | Bits, below | |
| `0xFF` | Firmware | max = firmware version, `sl` = panel id | INFERRED; the PG39WCDM reads max 103 |
| `0xEF` | ASUS support code and VCP table | Above | |

Every other monitor gets the standard codes only: `0x10`, `0x12`, `0x14`, `0x60` (MCCS names),
`0x62` and `0xD6`.

### `0xFD`, toggle word 2 (gaming line)

| Bit | Meaning | osdless | Evidence |
|---|---|---|---|
| `0x0001` | VRR | read | INFERRED |
| `0x0002` | ASCR | read | INFERRED |
| `0x0004` | Sniper | read | INFERRED |
| `0x0008` | OLED screen saver | `oled saver` | INFERRED |
| `0x0010` | Pixel cleaning running; setting it starts a run | `oled clean` | INFERRED |
| `0x0020` | OLED logo brightness | `oled logo` | INFERRED |
| `0x0040` | OLED uniform brightness | `oled uniform` | VERIFIED: written, read back and reverted on the PG39WCDM, `0x0029` → `0x0069` → `0x0029` |
| `0x0080` | Clear pixel edge | `oled clear-edge` | INFERRED |

`status` shows any other bit of the capabilities mask as unidentified. The PG39WCDM reads `0x0029`
(VRR, screen saver and logo brightness on) with max `0x00FD`, the same as its capabilities mask
(VERIFIED). On ProArt and other lines the same bits mean other things, such as HDR preview or
preset lock, and on gaming models that are not OLEDs `0x0040` is overclocking (INFERRED). That is
why osdless writes `0xFD` only on gaming OLEDs.

Writing a bit means reading the word, changing that one bit and writing the word back. A plain
`setvcp 0xFD 0x40` would also turn off every other bit.

### `0xFC`, toggle word 1 (read-only)

| Bit | Meaning | Evidence |
|---|---|---|
| `0x0001` | Power indicator (?) | THIRD-PARTY ([display-toolkit](https://github.com/feffelicious/display-toolkit), PG32UCWM) |
| `0x0002` | Power key lock (?) | THIRD-PARTY, same |
| `0x0004` | Key lock (?) | THIRD-PARTY, same |
| `0x0008` | Mute | INFERRED |
| `0x0010` | Auto input detection | INFERRED |
| `0x0080` | Unidentified; set on the PG39WCDM | VERIFIED as a reading only |
| `0x0400` | Unidentified | |

Because the low bits may lock the monitor's own buttons, osdless never writes `0xFC`.

## Replies to watch out for

| Reply | What it means | Evidence |
|---|---|---|
| cur `0x00FE`, max `0x00FE` | "Not available in the current mode": the setting is greyed out in the OSD. With VRR on, the PG39WCDM answers this for `0xE0`, `0xE3` to `0xE7`, `0xEA`, `0xEE`, `0xF4` and `0xFA` | VERIFIED |
| Any reply to `0xDD` | The PG39WCDM does not have `0xDD`, but answers with another code's data. A sweep that reads it looks like it worked | VERIFIED |
| No reply to `0xFE` | Times out after ddcutil's retries | VERIFIED |
| `0xFD` max | Equals the capabilities mask, `0x00FD` | VERIFIED |
| `0xFC` max | `0x849F`, not the capabilities mask `049F` | VERIFIED |
| `0xE2` = 0 | A real state that is not in its capabilities list | VERIFIED |
| "Unsupported feature code" | `0xE8` and `0xEB` are in the PG39WCDM's capabilities but answer this. They are commands, not settings, so osdless does not send them at all | VERIFIED |
| No capabilities | Some monitors do not answer the capabilities request while the panel is dimmed | THIRD-PARTY (display-toolkit) |

That is why osdless checks plausibility per code: a `0xFD` reply must carry the mask as its maximum
and no bit outside it, which a phantom answering with another code's data does not; a value reply
must be in the capabilities list. Decoding does not assume either, so `status` still shows `0xE2`
as "off". The `0xFD` rule is only known to hold on the PG39WCDM, which is one more reason other models
need `--untested`.

## Pixel cleaning

Setting `0xFD` bit `0x0010` starts a manual pixel cleaning run (INFERRED). The screen is
unavailable for several minutes, about 6 to 8 by THIRD-PARTY
[pg34ctl](https://github.com/Tiaxi/pg34ctl), and the run needs a live video signal, so the PC must
not sleep or blank the output (THIRD-PARTY
[asus-pixel-cleaner](https://codeberg.org/beeb/asus-pixel-cleaner)). The monitor is expected to clear
the bit when it is done, but on a PG32UCWM it does not do that reliably (THIRD-PARTY,
display-toolkit). Whether the PG39WCDM answers DDC/CI during a run, and whether the bit stays set
until the end, is not known yet: no run has been started through osdless. That is why osdless holds
its inhibitors for 11 minutes whether or not it polls, and does not let them go when the bit clears.

## Timing

| What | osdless | Why |
|---|---|---|
| Between ddcutil calls | at least 50 ms | DDC/CI, and ddcutil paces only within one process |
| Read-back after a setting | 200 ms | |
| Read-back after `kvm` or `input` | 2 s | A guess: the switch takes longer to show up, and sending it twice would switch again |
| `detect`, capabilities, a write | 30 s, 30 s, 10 s timeouts | Generous ceilings, since ddcutil waits on its lock while another program uses the monitor |
| A read | 10 s plus 2 s per code | Same |

## Sources

- VESA Monitor Control Command Set (MCCS) and the DDC/CI standard.
- The ddcutil 3.0.2 man page and [ddcutil.com](https://www.ddcutil.com).
- The Linux kernel's DRM code (`drm_sysfs.c`, `drm_dp_helper.c`, `intel_dp.c`), for which bus a
  DisplayPort connector's `ddc` symlink names.
- A read-only `ddcutil` 3.0.2 scan of a PG39WCDM on 2026-11-06, kept as
  [tests/fixtures/PG39WCDM.json](../tests/fixtures/PG39WCDM.json) with the serial number replaced.
- The codes and values ASUS's DisplayWidget Center 1.5 for Windows sends, studied for
  interoperability.
- [AsusDisplay](https://github.com/KharalDipendra/Asus-Displaywidget-Linux) (MIT), whose `caps.rs`
  publishes the capabilities strings the other fixtures use;
  [display-toolkit](https://github.com/feffelicious/display-toolkit);
  [pg34ctl](https://github.com/Tiaxi/pg34ctl);
  [asus-pixel-cleaner](https://codeberg.org/beeb/asus-pixel-cleaner).
