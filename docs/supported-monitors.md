# Supported monitors in detail

The README lists which monitors osdless supports. This page shows, per setting, what has been tried on
which model. Not in the table? Run `osdless features`: it says what your monitor offers, and why not.

## Support matrix

| Feature | PG39WCDM | PG34WCDM | PG32UCDM | PG49WCD | XG27ACDNG | Other ROG / TUF Gaming (table 2) | ProArt, other ASUS lines, older tables | Other makers |
|---|---|---|---|---|---|---|---|---|
| `list`, `status`, `features`, `report` | VERIFIED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED |
| `oled uniform` | VERIFIED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled saver` | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled logo` | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled clear-edge` | UNTESTED | UNTESTED | UNTESTED | UNTESTED | not listed | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled clean` | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled reminder` | UNTESTED | UNTESTED | UNTESTED | not listed | UNTESTED | UNTESTED on an OLED, if listed | read-only | read-only |
| `oled screen-move` | UNTESTED | UNTESTED | UNTESTED | not listed | not listed | UNTESTED on an OLED, if listed | read-only | read-only |
| `power` | UNTESTED | UNTESTED | UNTESTED | not listed | UNTESTED | UNTESTED if listed | read-only | read-only |
| `kvm` | UNTESTED | UNTESTED (usb-b, usb-c) | UNTESTED | not listed | UNTESTED (usb-b, usb-c) | UNTESTED if listed | read-only | read-only |
| `input` | UNTESTED | UNTESTED | UNTESTED | UNTESTED (dp, hdmi1, usbc) | UNTESTED | UNTESTED if listed | read-only | read-only |

- **VERIFIED** means it was done on a real monitor of that model. **UNTESTED** means osdless offers
  it, but a write needs `--untested MODEL`. **not listed** means the model's capabilities do not
  list the code, bit or any of its values, so osdless refuses it; **if listed** means it depends on
  that. **read-only** means osdless never writes to that monitor.
- **Tested on** one PG39WCDM (firmware 103, VCP table 2.7) over DisplayPort, with ddcutil 3.0.2,
  Linux 7.3 (CachyOS), amdgpu, KDE Plasma 6.8 and Python 3.14. osdless's `list`, `status`,
  `features` and `report` ran against it read-only. `get` uses the same read path but has only run
  against the test suite's fake ddcutil.
- **The one verified write.** `oled uniform on` and `off` were written, read back and reverted on that
  PG39WCDM on 2026-11-07 (`0xFD` `0x0029` → `0x0069` → `0x0029`) by the single-model script osdless
  grew out of, which has the same read-modify-write and read-back. osdless itself has not written to
  a real monitor yet.
- **The other named models** come from the fallback capabilities strings DisplayWidget Center uses
  when a monitor does not answer, as published in
  [AsusDisplay](https://github.com/KharalDipendra/Asus-Displaywidget-Linux)'s `caps.rs`, not from a
  real monitor. They show what osdless would offer if a real one answers the same way and reads
  `0xEF` as VCP table 2. The XG27ACDNG's `0xFD` mask also has bits above `0xFF`: while one of them is
  set, a toggle goes out as a 16-bit value, which has never been sent to any monitor.
- **Other ROG / TUF Gaming** covers every other `PG`, `XG` or `VG` model that reads `0xEF` as VCP
  table 2. The `oled` commands are offered only on a model ASUS's own app counts as an OLED, one
  whose 7th character is `D` or `W`, such as a PG27AQDM or a PG32UCWM (INFERRED). On a gaming LCD
  the same `0xFD` bits, `0xF8` and `0xF9` mean other things, such as overclocking, so osdless does
  not offer them there, not even with `--untested`.
- **ProArt, other ASUS lines and older VCP tables:** the same codes and bits mean other things there,
  so osdless only reads them. On those monitors and on **other makers**, `status` decodes the
  standard MCCS codes: brightness, contrast, colour preset, input, volume and power mode.
- **HDMI** has never been tried, for reads or writes. HDMI and DVI writes need `--untested` on every
  model. So does a monitor on HDMI behind a DisplayPort connector, through an active adapter or a
  dock: before a write over DisplayPort, osdless reads the monitor's own input (`0x60`), and
  anything but a DisplayPort input needs `--untested`. A monitor on the separate `ddc` adapter of a
  DisplayPort connector, as through a passive DisplayPort-to-HDMI adapter on amdgpu, is only read.
