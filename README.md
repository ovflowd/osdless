# osdless

[![CI](https://github.com/ovflowd/osdless/actions/workflows/ci.yml/badge.svg)](https://github.com/ovflowd/osdless/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Change your ASUS gaming monitor's settings from Linux without the OSD buttons: OLED care, power
saving, KVM and inputs.

ASUS only makes an app for these settings on Windows. osdless reads and changes them through
[ddcutil](https://www.ddcutil.com) and checks every change it makes. It's a single Python file.

> [!WARNING]
> osdless is built on reverse engineering ASUS's manufacturer-specific monitor settings, so a change
> can have unintended effects on your monitor. Use it at your own risk. This project isn't
> affiliated with ASUS.

## What you need

- Linux with [ddcutil](https://www.ddcutil.com) 2.1 or later
- Python 3.11 or later
- DDC/CI turned on in the monitor's OSD

## Install

It's one file, so you can read it before you run it:

```bash
curl -fLO https://raw.githubusercontent.com/ovflowd/osdless/main/osdless.py
python3 osdless.py status
```

Or install it as a command with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/ovflowd/osdless
```

## Usage

```bash
osdless status              # show the monitor's settings
osdless features            # what osdless can change on this monitor
osdless oled uniform on     # change a setting
osdless kvm usb-c --yes     # move the monitor's USB hub to the USB-C computer
```

| Command | What it does |
|---|---|
| `list` | Lists the monitors ddcutil finds |
| `status` | Shows the monitor's settings |
| `get CODE` | Reads one raw setting |
| `features` | Shows what osdless can change on this monitor, and why not |
| `report` | Writes a report that helps support a new monitor |
| `oled saver\|logo\|uniform\|clear-edge on\|off` | Screen saver, logo brightness, uniform brightness, clear pixel edge |
| `oled clean --yes` | Starts pixel cleaning; the screen stays dark for a few minutes |
| `oled reminder off\|2\|4\|8` | Pixel cleaning reminder, in hours |
| `oled screen-move off\|light\|middle\|strong` | Screen move |
| `power standard\|saving` | Power setting |
| `kvm smart\|usb-b\|usb-c --yes` | Which computer the monitor's USB hub follows |
| `input dp\|hdmi1\|hdmi2\|usbc --yes` | Input source |

| Option | |
|---|---|
| `--model MODEL`, `--serial SERIAL`, `--connector NAME`, `--bus N` | Choose the monitor when there's more than one |
| `--untested MODEL` | Allow a change nobody has tried on this model yet |
| `--yes` | Confirm `kvm`, `input` and `oled clean` |
| `--wait` | With `oled clean`, wait until cleaning ends |
| `--output DIR` | With `report`, where to write the file |
| `--json` | Print JSON |
| `--version` | Print the version |

## Supported monitors

| Monitor | Status |
|---|---|
| ROG Swift OLED PG39WCDM | Tested |
| ROG Swift OLED PG34WCDM | Untested |
| ROG Swift OLED PG32UCDM | Untested |
| ROG Swift OLED PG49WCD | Untested |
| ROG Strix OLED XG27ACDNG | Untested |
| Other ROG and TUF Gaming monitors | Untested |
| ProArt, other ASUS monitors and other brands | Read-only |

Untested means osdless supports the monitor, but nobody has tried it there yet, so changes need
`--untested <model>`. If you try one, please [report it](#reporting-a-monitor).
[docs/supported-monitors.md](docs/supported-monitors.md) has the details per setting.

## How it works

osdless only changes settings on monitors that identify themselves as ASUS gaming monitors. It changes
one setting at a time, starting from a fresh read, and reads it back afterwards. It never sends
resets, firmware commands or raw values. [docs/how-it-works.md](docs/how-it-works.md) has the
details, and [docs/protocol.md](docs/protocol.md) lists the settings.

## Reporting a monitor

```bash
osdless report
```

It reads your monitor's settings, changes nothing, and writes a JSON file without your serial number.
Attach it to a [monitor report](https://github.com/ovflowd/osdless/issues/new?template=monitor_report.yml).
[docs/adding-a-monitor.md](docs/adding-a-monitor.md) explains how a report turns into support.

Something not working? See [docs/troubleshooting.md](docs/troubleshooting.md).

## Related projects

- [ddcutil](https://www.ddcutil.com), which osdless runs, plus [ddcui](https://github.com/rockowitz/ddcui)
  and [vdu_controls](https://github.com/digitaltrails/vdu_controls) for the standard settings in a GUI.
- [AsusDisplay](https://github.com/KharalDipendra/Asus-Displaywidget-Linux) and
  [Sumi](https://github.com/Jenari-Dev/sumi), Linux GUIs for ASUS monitors.
- [pg34ctl](https://github.com/Tiaxi/pg34ctl), [asus-pixel-cleaner](https://codeberg.org/beeb/asus-pixel-cleaner)
  and [display-toolkit](https://github.com/feffelicious/display-toolkit), whose notes on other models
  are cited in [docs/protocol.md](docs/protocol.md).

No code was copied from any of them. Five test fixtures use capabilities strings from AsusDisplay's
`caps.rs` (MIT, Copyright (c) 2026 Dipendra Kharal).

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

The tests run against a fake ddcutil, so they need no monitor. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). ASUS, ROG, TUF Gaming and ProArt are trademarks of ASUSTeK Computer Inc., used here
only to describe compatibility.
