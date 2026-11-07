#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Change ASUS gaming monitor settings from Linux without the OSD, with checked writes on top of ddcutil.

osdless reads any DDC/CI monitor. It writes only a short, reviewed list of ASUS settings, and only to
a monitor whose own replies identify it as an ASUS gaming monitor with VCP table v2: one bit or one
listed value at a time, based on a fresh read, with a read-back after every write.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_READ_FAILED = 3
EXIT_WRITE_UNCONFIRMED = 4
EXIT_INTERRUPTED = 130

# MCCS action codes (0x01 degauss; 0x04, 0x05, 0x06, 0x08 and 0x0A restore defaults; 0xB0 stores or
# restores settings), the ASUS mode reset 0xEC, the ASUS firmware-update selector 0xFE, 0xDD, which
# an ASUS monitor can answer with another code's data, and the ASUS commands 0xE8 (a move) and 0xEB
# (OSD key presses), which are not settings. None of them is sent, not even as a read.
NEVER_SEND = frozenset({0x01, 0x04, 0x05, 0x06, 0x08, 0x0A, 0xB0, 0xEC, 0xFE, 0xDD, 0xE8, 0xEB})
# 0x10: the desktop owns brightness. 0xFC: its low bits may be the power-key and key locks.
# 0xE2/0xC5: the PC re-detects the display. 0xD6: panel off. 0xED, 0x47 and 0xF2 mean different
# things on different product lines.
NEVER_WRITE = NEVER_SEND | {0x10, 0xFC, 0xE2, 0xC5, 0xD6, 0xED, 0x47, 0xF2}

# A ddcutil configuration file could switch off its cross-instance lock, and user-supplied feature
# definitions could change how a code is read or written; neither may apply to osdless's calls.
DDCUTIL_OPTIONS = ("--noconfig", "--disable-udf", "--enable-cross-instance-locks")
DDCUTIL_READS = frozenset({"detect", "capabilities", "getvcp"})
DDCUTIL_WRITE = "setvcp"

# The support codes ASUS's own software accepts in 0xEF's current value.
ASUS_SUPPORT_CODES = frozenset({0x1A}).union(
    *(range(low, low + 4) for low in (0x55, 0x66, 0x77, 0x88, 0x99, 0xA0, 0xB1, 0xC2, 0xD3, 0xE4, 0xF5))
)
ASUS_GAMING_PREFIXES = ("PG", "XG", "VG")
# ASUS's own app treats a gaming model as an OLED when the 7th character of its name is one of these.
# Outside the OLEDs, 0xFD's bits mean other things (bit 0x0040 is overclocking on some models).
ASUS_OLED_LETTERS = frozenset("DW")
# Read before the capabilities are known, because they key the capabilities cache.
BOOTSTRAP = (0xFF, 0xEF)
# Only on these models is 0xFF's max known to be the firmware version, so only their capabilities
# are cached under it.
CACHED_MODELS = frozenset({"PG39WCDM"})
# (EDID model, feature) pairs that were written, read back and reverted on that model through this write path.
VERIFIED = frozenset({("PG39WCDM", "oled uniform")})

FD_PIXEL_CLEANING = 0x0010
REDACTED = "<redacted>"
REPORT_SCHEMA = 1
REPORT_BATCH = 8

# DDC/CI wants >= 50 ms between commands; ddcutil paces only within one process.
MIN_GAP = 0.05
SETTLE = 0.2
# A USB hub move or input switch takes longer to show up than a setting, and sending it twice would
# disrupt the switch again, so these are read back later and never resent.
SWITCH_SETTLE = 2.0
# ddcutil queues on its cross-instance lock while the desktop's power manager talks to the monitor,
# so these are generous ceilings, not expected durations.
TIMEOUT_DETECT = 30.0
TIMEOUT_CAPS = 30.0
TIMEOUT_GET_BASE = 10.0
TIMEOUT_GET_PER_CODE = 2.0
TIMEOUT_SET = 10.0
TIMEOUT_VERSION = 10.0
CLEAN_GUARD = 20 * 3600
POLL_INTERVAL = 30.0
WAIT_CAP = 10 * 60
INHIBIT_SETTLE = 0.3
# PowerDevil lists a new inhibition about 5 s after it is requested (Plasma 6).
KDE_REGISTER_WAIT = 10.0
KDE_POLL = 0.5
# PowerDevil's types for kde-inhibit's --power and --screenSaver inhibitions.
KDE_INHIBIT_TYPES = ("sleep", "idle")

CLEAN_WARNING = (
    "pixel cleaning makes the screen unavailable for about 6 to 8 minutes; keep the monitor on and the PC awake"
)
KVM_WARNING = "switching KVM moves the monitor's USB hub, and every USB device on it, to the other host"
INPUT_WARNING = "switching input blanks this screen until something switches it back"

MCCS_PRESETS = (
    (0x01, "sRGB"),
    (0x02, "Display native"),
    (0x03, "4000 K"),
    (0x04, "5000 K"),
    (0x05, "6500 K"),
    (0x06, "7500 K"),
    (0x07, "8200 K"),
    (0x08, "9300 K"),
    (0x09, "10000 K"),
    (0x0A, "11500 K"),
    (0x0B, "User 1"),
    (0x0C, "User 2"),
    (0x0D, "User 3"),
)
MCCS_INPUTS = (
    (0x01, "VGA-1"),
    (0x02, "VGA-2"),
    (0x03, "DVI-1"),
    (0x04, "DVI-2"),
    (0x0F, "DisplayPort-1"),
    (0x10, "DisplayPort-2"),
    (0x11, "HDMI-1"),
    (0x12, "HDMI-2"),
)
POWER_MODES = ((0x01, "on"), (0x02, "standby"), (0x03, "suspend"), (0x04, "off"), (0x05, "off (power button)"))
ASUS_INPUTS = ((0x0F, "DisplayPort"), (0x11, "HDMI-1"), (0x12, "HDMI-2"), (0x1A, "USB-C"))
# The link each 0x60 input implies. An active adapter or a dock can put a monitor's HDMI input behind
# a DisplayPort connector, which sysfs alone cannot tell.
INPUT_LINKS = {0x0F: "DisplayPort", 0x10: "DisplayPort", 0x11: "HDMI", 0x12: "HDMI", 0x1A: "USB-C"}
GAMEVISUAL = (
    (0x01, "Cinema"),
    (0x02, "Scenery"),
    (0x03, "sRGB"),
    (0x04, "User"),
    (0x05, "Racing"),
    (0x06, "RTS"),
    (0x07, "FPS"),
    (0x08, "MOBA"),
    (0x09, "Night Vision"),
    (0x0A, "sRGB Cal"),
)
POWER_NAMES = ((0, "Standard"), (1, "Power Saving"))
HDR_NAMES = (
    (0, "off"),
    (1, "Gaming HDR (order inferred)"),
    (2, "Cinema HDR (order inferred)"),
    (3, "Console HDR (order inferred)"),
)
KVM_NAMES = ((1, "Smart KVM"), (2, "USB-B upstream"), (3, "USB-C upstream"), (4, "Auto KVM"))
REMINDER_NAMES = ((0, "off"), (2, "every 2 h"), (4, "every 4 h"), (8, "every 8 h"))
SCREEN_MOVE_NAMES = ((0, "off"), (1, "light"), (2, "middle"), (3, "strong"))
SHORTCUTS = (
    (1, "GamePlus"),
    (2, "GameVisual"),
    (3, "Brightness"),
    (4, "Mute"),
    (5, "Shadow Boost"),
    (6, "Contrast"),
    (7, "Input Select"),
    (8, "HDR"),
    (9, "Blue Light Filter"),
    (10, "Colour Temp"),
    (11, "Volume"),
    (12, "KVM"),
    (13, "Smart KVM"),
    (14, "Frame Rate Boost"),
    (15, "Pixel Cleaning"),
    (16, "Customized 1"),
    (17, "Customized 2"),
)
FD_BITS = (
    (0x0001, "VRR"),
    (0x0002, "ASCR"),
    (0x0004, "Sniper"),
    (0x0008, "screen saver"),
    (0x0010, "pixel cleaning running"),
    (0x0020, "logo brightness"),
    (0x0040, "uniform brightness"),
    (0x0080, "clear pixel edge"),
)
# Only mute and auto input detection come from ASUS's own software; the rest are guesses from other models.
FC_BITS = (
    (0x0001, "power indicator (?)"),
    (0x0002, "power key lock (?)"),
    (0x0004, "key lock (?)"),
    (0x0008, "mute"),
    (0x0010, "auto input detection"),
)

# Inhibitor processes left running on purpose after a clean; kept referenced so they are not
# garbage-collected while alive.
DETACHED: list[subprocess.Popen[bytes]] = []


# --- Data model --------------------------------------------------------------------------------


class Failure(Exception):
    exit_code = EXIT_REFUSED


class Refused(Failure):
    exit_code = EXIT_REFUSED


class ReadFailed(Failure):
    exit_code = EXIT_READ_FAILED


class WriteUnconfirmed(Failure):
    exit_code = EXIT_WRITE_UNCONFIRMED


@dataclass(frozen=True)
class Reading:
    code: int
    mh: int
    ml: int
    sh: int
    sl: int

    @property
    def cur(self) -> int:
        return self.sh << 8 | self.sl

    @property
    def max(self) -> int:
        return self.mh << 8 | self.ml

    @property
    def not_available(self) -> bool:
        return self.cur == 0x00FE and self.max == 0x00FE


@dataclass(frozen=True)
class Caps:
    raw: str
    model: str
    mccs_version: str
    vcp: dict[int, tuple[int, ...]]

    def has(self, code: int) -> bool:
        return code in self.vcp

    def values(self, code: int) -> tuple[int, ...]:
        return self.vcp.get(code, ())

    def mask(self, code: int) -> int:
        values = self.vcp.get(code, ())
        return values[0] if len(values) == 1 else 0

    def listed(self, code: int) -> str:
        return f"{code:02X}({' '.join(f'{v:02X}' for v in self.values(code))})"


@dataclass(frozen=True)
class WriteResult:
    old: int
    new: int
    changed: bool


@dataclass(frozen=True)
class Display:
    bus: int
    connector: str
    mfg: str
    model: str
    serial: str

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.mfg, self.model, self.serial

    def name(self) -> str:
        return f"{self.mfg}:{self.model}:{self.serial}"


@dataclass(frozen=True)
class Link:
    connector: str
    transport: str

    @property
    def card(self) -> str:
        return self.connector.split("-", 1)[0]


class Kind(Enum):
    RAW = "raw"
    LEVEL = "level"
    NAMES = "names"
    PRESET = "preset"
    GAMMA = "gamma"
    SHORTCUTS = "shortcuts"
    BITS = "bits"
    FIRMWARE = "firmware"
    VCP_TABLE = "vcp-table"


@dataclass(frozen=True)
class Code:
    code: int
    label: str
    kind: Kind = Kind.RAW
    names: tuple[tuple[int, str], ...] = ()
    note: str = ""


class Write(Enum):
    BIT = "bit"
    VALUE = "value"
    SWITCH = "switch"
    CLEAN = "action"


@dataclass(frozen=True)
class Feature:
    command: tuple[str, ...]
    code: int
    write: Write
    bit: int = 0
    choices: tuple[tuple[str, int], ...] = ()
    summary: str = ""
    warning: str = ""

    @property
    def name(self) -> str:
        return " ".join(self.command)

    @property
    def oled(self) -> bool:
        return self.command[0] == "oled"


@dataclass(frozen=True)
class Profile:
    name: str
    writes: bool
    status: tuple[Code, ...]
    features: tuple[Feature, ...]
    # Codes whose capabilities entry is one 16-bit mask of supported bits, not a list of values.
    masked: frozenset[int]


@dataclass(frozen=True)
class Availability:
    status: str
    reason: str
    values: tuple[str, ...]


ASUS_STATUS = (
    Code(0x10, "Brightness", Kind.LEVEL, note="never written: the desktop owns brightness"),
    Code(0x12, "Contrast", Kind.LEVEL),
    Code(0x14, "Colour temperature", Kind.PRESET, MCCS_PRESETS),
    Code(0x60, "Input", Kind.NAMES, ASUS_INPUTS),
    Code(0x62, "Volume", Kind.LEVEL),
    Code(0x72, "Gamma", Kind.GAMMA),
    Code(0xDC, "GameVisual", Kind.NAMES, GAMEVISUAL),
    Code(0xE1, "Power setting", Kind.NAMES, POWER_NAMES),
    Code(0xE2, "HDR", Kind.NAMES, HDR_NAMES),
    Code(0xF3, "KVM", Kind.NAMES, KVM_NAMES),
    Code(0xF8, "Cleaning reminder", Kind.NAMES, REMINDER_NAMES),
    Code(0xF9, "Screen move", Kind.NAMES, SCREEN_MOVE_NAMES),
    Code(0xFB, "Shortcuts", Kind.SHORTCUTS, SHORTCUTS),
    Code(0xFC, "Toggles 1", Kind.BITS, FC_BITS, note="read-only"),
    Code(0xFD, "OLED care", Kind.BITS, FD_BITS),
    Code(0xFF, "Firmware", Kind.FIRMWARE),
    Code(0xEF, "ASUS VCP id", Kind.VCP_TABLE),
)
MCCS_STATUS = (
    Code(0x10, "Brightness", Kind.LEVEL, note="never written: the desktop owns brightness"),
    Code(0x12, "Contrast", Kind.LEVEL),
    Code(0x14, "Colour preset", Kind.PRESET, MCCS_PRESETS),
    Code(0x60, "Input", Kind.NAMES, MCCS_INPUTS),
    Code(0x62, "Volume", Kind.LEVEL),
    Code(0xD6, "Power mode", Kind.NAMES, POWER_MODES),
)

FEATURES = (
    Feature(("oled", "saver"), 0xFD, Write.BIT, bit=0x0008, summary="screen saver (0xFD bit 0x0008)"),
    Feature(("oled", "logo"), 0xFD, Write.BIT, bit=0x0020, summary="logo brightness (0xFD bit 0x0020)"),
    Feature(("oled", "uniform"), 0xFD, Write.BIT, bit=0x0040, summary="uniform brightness (0xFD bit 0x0040)"),
    Feature(("oled", "clear-edge"), 0xFD, Write.BIT, bit=0x0080, summary="clear pixel edge (0xFD bit 0x0080)"),
    Feature(
        ("oled", "clean"),
        0xFD,
        Write.CLEAN,
        bit=FD_PIXEL_CLEANING,
        summary="start manual pixel cleaning (0xFD bit 0x0010)",
        warning=CLEAN_WARNING,
    ),
    Feature(
        ("oled", "reminder"),
        0xF8,
        Write.VALUE,
        choices=(("off", 0), ("2", 2), ("4", 4), ("8", 8)),
        summary="pixel cleaning reminder, in hours (0xF8)",
    ),
    Feature(
        ("oled", "screen-move"),
        0xF9,
        Write.VALUE,
        choices=(("off", 0), ("light", 1), ("middle", 2), ("strong", 3)),
        summary="screen move (0xF9)",
    ),
    Feature(("power",), 0xE1, Write.VALUE, choices=(("standard", 0), ("saving", 1)), summary="Power Setting (0xE1)"),
    Feature(
        ("kvm",),
        0xF3,
        Write.SWITCH,
        choices=(("smart", 1), ("usb-b", 2), ("usb-c", 3)),
        summary="KVM upstream (0xF3)",
        warning=KVM_WARNING,
    ),
    Feature(
        ("input",),
        0x60,
        Write.SWITCH,
        choices=(("dp", 0x0F), ("hdmi1", 0x11), ("hdmi2", 0x12), ("usbc", 0x1A)),
        summary="input source (0x60)",
        warning=INPUT_WARNING,
    ),
)
CLEAN = next(feature for feature in FEATURES if feature.write is Write.CLEAN)
BIT_WRITES = frozenset({Write.BIT, Write.CLEAN})
BITFIELDS = frozenset(feature.code for feature in FEATURES if feature.write in BIT_WRITES)
GROUP_HELP = {"oled": "OLED care settings"}

ASUS_V2_GAMING = Profile("asus-v2-gaming", True, ASUS_STATUS, FEATURES, frozenset({0xFC, 0xFD}))
READ_ONLY = Profile("read-only", False, MCCS_STATUS, (), frozenset())


# --- Parsers -----------------------------------------------------------------------------------


def parse_caps(raw: str) -> Caps:
    model = re.search(r"model\(([^()]*)\)", raw)
    start = raw.find("vcp(")
    if model is None or start < 0:
        raise ReadFailed("the capabilities string has no model() or vcp() section")
    mccs = re.search(r"mccs_ver\(([^()]*)\)", raw)
    vcp: dict[int, tuple[int, ...]] = {}
    depth = 1
    code: int | None = None
    values: list[int] = []
    for token in re.finditer(r"\s+|\(|\)|[0-9A-Fa-f]+|.", raw[start + 4 :]):
        text = token.group()
        if text.isspace():
            continue
        if text == "(":
            if depth != 1 or code is None:
                raise ReadFailed(f"unexpected '(' in the capabilities vcp() list near {token.start()}")
            depth = 2
            values = []
        elif text == ")":
            if depth == 2 and code is not None:
                vcp[code] = tuple(values)
                depth = 1
            else:
                break
        elif re.fullmatch(r"[0-9A-Fa-f]+", text):
            if depth == 1:
                if len(text) != 2:
                    raise ReadFailed(f"capabilities vcp() list has a malformed code {text!r}")
                code = int(text, 16)
                vcp[code] = ()
            else:
                values.append(int(text, 16))
        else:
            raise ReadFailed(f"capabilities vcp() list has an unexpected character {text!r}")
    else:
        raise ReadFailed("the capabilities vcp() list is not closed")
    return Caps(raw=raw, model=model.group(1).strip(), mccs_version=mccs.group(1).strip() if mccs else "", vcp=vcp)


RAW_VALUE = re.compile(
    r"^Raw value: opcode=0x([0-9a-f]{2}), mh=0x([0-9a-f]{2}), ml=0x([0-9a-f]{2}), "
    r"sh=0x([0-9a-f]{2}), sl=0x([0-9a-f]{2})\b",
    re.I | re.M,
)
VCP_LINE = re.compile(r"^VCP code 0x([0-9a-f]{2}) \(.*?\):\s*(.*)$", re.I | re.M)


def parse_getvcp(output: str, codes: list[int]) -> dict[int, Reading | str]:
    """Maps each requested code to its Reading, or to the reason it has none."""
    found: dict[int, Reading | str] = {}
    replies: Counter[int] = Counter()
    for match in RAW_VALUE.finditer(output):
        code = int(match.group(1), 16)
        replies[code] += 1
        mh, ml, sh, sl = (int(group, 16) for group in match.groups()[1:])
        found[code] = Reading(code, mh, ml, sh, sl)
    for match in VCP_LINE.finditer(output):
        code = int(match.group(1), 16)
        found.setdefault(code, match.group(2).strip() or "no value")
    results: dict[int, Reading | str] = {}
    for code in codes:
        results[code] = "more than one reply" if replies[code] > 1 else found.get(code, "no reply")
    return results


def parse_detect(output: str) -> list[Display]:
    displays: list[Display] = []
    fields: dict[str, str] | None = None

    def finish() -> None:
        if fields and "bus" in fields and "mfg" in fields:
            displays.append(
                Display(
                    int(fields["bus"]), fields.get("connector", ""), fields["mfg"], fields["model"], fields["serial"]
                )
            )

    for line in output.splitlines():
        if re.fullmatch(r"Display \d+\s*", line):
            finish()
            fields = {}
            continue
        if line[:1].strip():
            # "Invalid display", "Phantom display" and the like: nothing osdless may talk to.
            finish()
            fields = None
            continue
        if fields is None:
            continue
        if match := re.fullmatch(r"\s+I2C bus:\s+/dev/i2c-(\d+)\s*", line):
            fields["bus"] = match.group(1)
        elif match := re.fullmatch(r"\s+DRM connector:\s+(\S+)\s*", line):
            fields["connector"] = match.group(1)
        elif match := re.fullmatch(r"\s+Monitor:\s+([^:]*):([^:]*):(.*?)\s*", line):
            fields["mfg"], fields["model"], fields["serial"] = match.groups()
    finish()
    return displays


def parse_code(text: str) -> int:
    match = re.fullmatch(r"(?:0[xX])?([0-9A-Fa-f]{1,2})", text.strip())
    if match is None:
        raise Refused(f"{text!r} is not a VCP code (expected hex, e.g. FD or 0xFD)")
    return int(match.group(1), 16)


# --- ddcutil -----------------------------------------------------------------------------------


def ddcutil_binary() -> str:
    return os.environ.get("OSDLESS_DDCUTIL") or shutil.which("ddcutil") or "ddcutil"


def ddcutil_said(status: int, output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return f"exit {status}: {' '.join(lines[:2])}" if lines else f"exit {status}, no output"


class Ddcutil:
    """Runs ddcutil one call at a time, with a timeout per call and its cross-instance lock kept on.

    A runner built with writes=False cannot send setvcp at all; every read-only command uses one.
    """

    def __init__(self, *, writes: bool = False) -> None:
        self.writes = writes
        self._last_end = 0.0

    def run(self, subcommand: str, args: list[str], timeout: float) -> tuple[int | None, str]:
        """Returns ddcutil's exit status (None when the call timed out) and its merged output."""
        allowed = DDCUTIL_READS | {DDCUTIL_WRITE} if self.writes else DDCUTIL_READS
        if subcommand not in allowed:
            if subcommand == DDCUTIL_WRITE:
                raise Refused("this command only reads, so it cannot send 'ddcutil setvcp'")
            raise Refused(f"'ddcutil {subcommand}' is not something osdless sends")
        return self._call([subcommand, *args], timeout)

    def version(self) -> str:
        status, output = self._call(["--version"], TIMEOUT_VERSION)
        match = re.match(r"ddcutil (\S+)", output)
        return match.group(1) if status == 0 and match else "unknown"

    def _call(self, words: list[str], timeout: float) -> tuple[int | None, str]:
        wait = MIN_GAP - (time.monotonic() - self._last_end)
        if wait > 0:
            time.sleep(wait)
        argv = [ddcutil_binary(), *DDCUTIL_OPTIONS, *words]
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                # ddcutil prints FC/FD's 16-bit masks as raw bytes in its parsed capabilities.
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, ""
        except OSError as exc:
            raise ReadFailed(f"cannot run {argv[0]}: {exc}") from exc
        finally:
            self._last_end = time.monotonic()
        return proc.returncode, proc.stdout


# --- sysfs and choosing a monitor ---------------------------------------------------------------

TRANSPORTS = {
    "DP": "DisplayPort",
    "eDP": "DisplayPort",
    "HDMI-A": "HDMI",
    "HDMI-B": "HDMI",
    "DVI-D": "DVI",
    "DVI-I": "DVI",
}


@dataclass(frozen=True)
class DrmMap:
    links: dict[int, Link]
    # A connected DisplayPort connector's `ddc` adapter when that is not its own DP-AUX adapter: on
    # amdgpu, where a passive DisplayPort-to-HDMI adapter puts the monitor's DDC/CI.
    dp_ddc: dict[int, str]


def drm_root() -> Path:
    return Path(os.environ.get("OSDLESS_DRM_ROOT") or "/sys/class/drm")


def adapter_number(name: str) -> int | None:
    match = re.fullmatch(r"i2c-(\d+)", name)
    return int(match.group(1)) if match else None


def map_connectors() -> DrmMap:
    links: dict[int, Link] = {}
    dp_ddc: dict[int, str] = {}
    for connector in sorted(drm_root().glob("card*-*")):
        kind = re.fullmatch(r"card\d+-(.+)-\d+", connector.name)
        transport = TRANSPORTS.get(kind.group(1)) if kind else None
        if transport is None:
            continue
        try:
            if (connector / "status").read_text().strip() != "connected":
                continue
            ddc = (
                adapter_number(Path(os.readlink(connector / "ddc")).name) if (connector / "ddc").is_symlink() else None
            )
        except OSError:
            continue
        if transport == "DisplayPort":
            # DDC/CI goes over the connector's own i2c-N directory, the DP-AUX adapter. i915 points
            # the `ddc` symlink at that same adapter; amdgpu points it at another one.
            aux: set[int] = set()
            for adapter in connector.glob("i2c-*"):
                bus = adapter_number(adapter.name)
                if bus is not None and adapter.is_dir() and not adapter.is_symlink():
                    links[bus] = Link(connector.name, transport)
                    aux.add(bus)
            if ddc is not None and ddc not in aux:
                dp_ddc[ddc] = connector.name
        elif ddc is not None:
            links[ddc] = Link(connector.name, transport)
    return DrmMap(links, dp_ddc)


@dataclass(frozen=True)
class Selector:
    serial: str | None = None
    model: str | None = None
    connector: str | None = None
    bus: int | None = None

    @property
    def by_location(self) -> bool:
        return self.connector is not None or self.bus is not None

    def matches(self, display: Display, connector: str) -> bool:
        if self.serial is not None and display.serial != self.serial:
            return False
        if self.model is not None and display.model.upper() != self.model.upper():
            return False
        if self.bus is not None and display.bus != self.bus:
            return False
        return self.connector is None or self.connector in (connector, connector.split("-", 1)[-1])

    def describe(self) -> str:
        given = [(name, value) for name, value in vars(self).items() if value is not None]
        return " ".join(f"--{name} {value}" for name, value in given) or "the selectors"


def connector_of(display: Display, drm: DrmMap) -> str:
    link = drm.links.get(display.bus)
    return display.connector or (link.connector if link else drm.dp_ddc.get(display.bus, ""))


def detect_displays(ddc: Ddcutil) -> list[Display]:
    status, output = ddc.run("detect", ["--terse"], TIMEOUT_DETECT)
    if status is None:
        raise ReadFailed("ddcutil detect timed out")
    displays = parse_detect(output)
    if displays:
        return displays
    # ddcutil detect exits 0 whenever it ran, so anything else is ddcutil itself failing, such as an
    # older ddcutil rejecting one of DDCUTIL_OPTIONS, and not a missing monitor.
    if status != 0:
        raise ReadFailed(f"ddcutil detect failed ({ddcutil_said(status, output)})")
    raise ReadFailed(
        "ddcutil detect found no DDC/CI display; is the monitor on, on this input, with DDC/CI enabled in its OSD?"
    )


def select(displays: list[Display], selector: Selector, drm: DrmMap) -> Display:
    matches = [display for display in displays if selector.matches(display, connector_of(display, drm))]
    if not matches:
        raise ReadFailed(f"no detected display matches {selector.describe()}; `osdless list` shows what ddcutil finds")
    if len(matches) > 1:
        # No serial numbers here: this refusal can come from any command, `report` included.
        listed = "; ".join(
            f"{display.model} on /dev/i2c-{display.bus} ({connector_of(display, drm) or 'no DRM connector'})"
            for display in matches
        )
        raise Refused(
            f"{len(matches)} displays match, so choose one with --serial, --model, --connector or --bus "
            f"(`osdless list` shows their serial numbers): {listed}"
        )
    return matches[0]


# --- Files, lock and journal -------------------------------------------------------------------


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def xdg_dir(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable, "")
    # The XDG spec says relative values are invalid and must be ignored.
    base = Path(value) if os.path.isabs(value) else Path.home() / fallback
    return base / "osdless"


def make_private_dir(path: Path) -> None:
    # The cache files hold the monitor's serial. An existing directory is left as its owner set it.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def clean_state_file() -> Path:
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "last-clean.json"


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text) or "_"


def warn(message: str) -> None:
    print(f"osdless: {message}", file=sys.stderr)


def syslog(message: str) -> None:
    if os.environ.get("OSDLESS_NO_SYSLOG") == "1":
        return
    logger = shutil.which("logger")
    if logger is None:
        warn(f"logger not found, so this write is not in the journal: {message}")
        return
    try:
        subprocess.run(
            [logger, "-t", "osdless", "--", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"logger failed ({exc}), so this write is not in the journal: {message}")


def acquire_lock() -> int:
    path = runtime_dir() / "osdless.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise Refused(f"cannot open the lock file {path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise Refused(f"another osdless run holds {path}") from None
    return fd


def load_cached_caps(path: Path, display: Display, ff: Reading, ef: Reading) -> Caps | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("mfg"), data.get("model"), data.get("serial")) != display.identity:
        return None
    if data.get("ff_max") != ff.max or data.get("ef") != [ef.cur, ef.max]:
        return None
    raw = data.get("capabilities")
    if not isinstance(raw, str):
        return None
    try:
        return parse_caps(raw)
    except ReadFailed:
        return None


def store_cached_caps(path: Path, caps: Caps, display: Display, ff: Reading, ef: Reading) -> None:
    data = {
        "mfg": display.mfg,
        "model": display.model,
        "serial": display.serial,
        "ff_max": ff.max,
        "ef": [ef.cur, ef.max],
        "capabilities": caps.raw,
        "read_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        make_private_dir(path.parent)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        warn(f"could not cache the capabilities in {path}: {exc}")


# --- Profiles ----------------------------------------------------------------------------------


def asus_support_code(reading: Reading | str | None) -> TypeGuard[Reading]:
    return isinstance(reading, Reading) and not reading.not_available and reading.cur in ASUS_SUPPORT_CODES


def asus_oled(model: str) -> bool:
    name = model.upper()
    return name.startswith(ASUS_GAMING_PREFIXES) and len(name) > 6 and name[6] in ASUS_OLED_LETTERS


def not_oled(model: str) -> str:
    return (
        f"{model} is not an OLED by ASUS's naming rule (7th character D or W), and on other models 0xFD's bits, "
        "0xF8 and 0xF9 mean other things"
    )


def choose_profile(display: Display, caps: Caps, ef: Reading | str | None) -> tuple[Profile, tuple[str, ...]]:
    """Picks the profile from the monitor's own replies, with one line per step for the user."""
    if display.mfg != "AUS":
        return READ_ONLY, (f"EDID maker {display.mfg or '(none)'} is not AUS; osdless writes only to ASUS monitors",)
    reasons = ["EDID maker AUS"]
    if not caps.has(0xEF):
        return READ_ONLY, (*reasons, "the capabilities do not list 0xEF, the ASUS support code")
    if not isinstance(ef, Reading):
        return READ_ONLY, (*reasons, f"reading 0xEF failed: {ef or 'not read'}")
    if not asus_support_code(ef):
        return READ_ONLY, (*reasons, f"0xEF reads 0x{ef.cur:04X} (max 0x{ef.max:04X}), not an ASUS support code")
    reasons.append(f"0xEF support code 0x{ef.cur:02X}, VCP table {ef.mh}.{ef.ml}")
    if ef.mh != 2:
        return READ_ONLY, (
            *reasons,
            f"VCP table {ef.mh}.{ef.ml} is not table v2, and 0xE1, 0xF8 and 0xF9 mean other things on other tables",
        )
    if not display.model.upper().startswith(ASUS_GAMING_PREFIXES):
        return READ_ONLY, (
            *reasons,
            f"{display.model} is not in the ASUS gaming line (PG, XG, VG), where 0xFD's bits mean other things",
        )
    reasons.append(f"{display.model} is in the ASUS gaming line")
    if caps.model.upper() != display.model.upper():
        return READ_ONLY, (*reasons, f"the capabilities name model {caps.model!r}, not {display.model}")
    reasons.append("the capabilities name the same model")
    return ASUS_V2_GAMING, tuple(reasons)


# --- The monitor -------------------------------------------------------------------------------


class Monitor:
    def __init__(self, ddc: Ddcutil, selector: Selector | None = None, untested: str | None = None) -> None:
        self.ddc = ddc
        self.selector = selector or Selector()
        self.untested = untested
        self.display: Display | None = None
        self.link: Link | None = None
        # Why the way the monitor was chosen rules out writes, if it does.
        self.write_block = ""
        self.caps: Caps | None = None
        self.caps_source = ""
        self.early: dict[int, Reading | str] = {}
        self.profile = READ_ONLY
        self.reasons: tuple[str, ...] = ("not connected yet",)
        # The DisplayPort connector whose separate `ddc` adapter the monitor answers on, if it does.
        self.ddc_of: str | None = None
        # Why the monitor's own input (0x60) makes a DisplayPort write untested: "" when it reads
        # DisplayPort, None until it has been read.
        self.input_reason: str | None = None
        # Last successful read per code; a write must consume one, so read-modify-write can never
        # start from a failed or missing read.
        self._fresh: dict[int, int] = {}

    @property
    def bus(self) -> int | None:
        return self.display.bus if self.display else None

    def connect(self) -> None:
        self.identify()
        caps = self.load_caps()
        display = self.need_display()
        self.profile, self.reasons = choose_profile(display, caps, self.early.get(0xEF))

    def need_display(self) -> Display:
        if self.display is None:
            raise Refused("the monitor has not been identified")
        return self.display

    def need_caps(self) -> Caps:
        if self.caps is None:
            raise Refused("the monitor's capabilities are not known")
        return self.caps

    def identify(self) -> None:
        drm = map_connectors()
        display = select(detect_displays(self.ddc), self.selector, drm)
        link = drm.links.get(display.bus)
        ddc_of = None if link else drm.dp_ddc.get(display.bus)
        sysfs = link.connector if link else ddc_of
        if sysfs is not None and display.connector and display.connector != sysfs:
            raise Refused(f"ddcutil puts i2c-{display.bus} on {display.connector}, sysfs on {sysfs}")
        if self.untested is not None and self.untested.upper() != display.model.upper():
            raise Refused(f"--untested names {self.untested}, but the chosen monitor is a {display.model}")
        self.display, self.link, self.ddc_of = display, link, ddc_of
        if not display.serial and not self.selector.by_location:
            self.write_block = (
                "the monitor reports no serial number, so writes need it chosen with --connector or --bus"
            )

    def load_caps(self) -> Caps:
        display = self.need_display()
        path: Path | None = None
        if display.mfg == "AUS" and display.model.upper() in CACHED_MODELS and display.serial:
            self.early = self.read(list(BOOTSTRAP), bootstrap=True)
            for code, reading in self.early.items():
                if not isinstance(reading, Reading):
                    raise ReadFailed(f"0x{code:02X}: {reading}")
            path = self.cache_path()
        ff, ef = self.early.get(0xFF), self.early.get(0xEF)
        caps = None
        if path is not None and isinstance(ff, Reading) and isinstance(ef, Reading):
            caps = load_cached_caps(path, display, ff, ef)
            self.caps_source = f"cached in {path}"
        if caps is None:
            caps = self.read_live_caps()
            self.caps_source = "read live"
            if path is not None and isinstance(ff, Reading) and isinstance(ef, Reading):
                store_cached_caps(path, caps, display, ff, ef)
        self.caps = caps
        if display.mfg == "AUS" and 0xEF not in self.early and caps.has(0xEF):
            self.early.update(self.read([0xEF]))
        return caps

    def cache_path(self) -> Path | None:
        display = self.need_display()
        ff, ef = self.early.get(0xFF), self.early.get(0xEF)
        # A phantom or not-available reply must not key the cache.
        if not isinstance(ff, Reading) or ff.not_available or not asus_support_code(ef):
            return None
        name = f"caps-{display.mfg}-{display.model}-{display.serial}-ff{ff.max:04X}-ef{ef.cur:04X}{ef.max:04X}"
        return xdg_dir("XDG_CACHE_HOME", ".cache") / f"{safe_name(name)}.json"

    def read_live_caps(self) -> Caps:
        # ddcutil's own cache is keyed without the firmware version, so a miss in osdless's cache
        # (new firmware or VCP table) must not be answered from it.
        status, output = self.ddc.run(
            "capabilities", ["--bus", str(self.bus), "--verbose", "--disable-capabilities-cache"], TIMEOUT_CAPS
        )
        if status is None:
            raise ReadFailed("ddcutil capabilities timed out")
        match = re.search(r"^Unparsed capabilities string:\s*(\(.*\))\s*$", output, re.M)
        if match is None and status != 0:
            raise ReadFailed(f"ddcutil capabilities failed ({ddcutil_said(status, output)})")
        if match is None:
            raise ReadFailed("ddcutil capabilities returned no capabilities string")
        return parse_caps(match.group(1))

    def _check_read(self, code: int, bootstrap: bool) -> None:
        if code in NEVER_SEND:
            raise Refused(f"0x{code:02X} is on the never-send list")
        if self.bus is None:
            raise Refused("the monitor has not been identified")
        if bootstrap:
            if code not in BOOTSTRAP:
                raise Refused(f"0x{code:02X} cannot be read before the capabilities are known")
        elif self.caps is None or not self.caps.has(code):
            raise Refused(f"0x{code:02X} is not in the monitor's capabilities")

    def read(self, codes: list[int], *, bootstrap: bool = False) -> dict[int, Reading | str]:
        if not codes:
            return {}
        for code in codes:
            self._check_read(code, bootstrap)
        status, output = self.ddc.run(
            "getvcp",
            ["--bus", str(self.bus), "--verbose", *(f"0x{code:02X}" for code in codes)],
            TIMEOUT_GET_BASE + TIMEOUT_GET_PER_CODE * len(codes),
        )
        if status is None:
            return dict.fromkeys(codes, "timed out")
        return parse_getvcp(output, codes)

    def try_read(self, code: int) -> Reading | str:
        return self.read([code])[code]

    def read_one(self, code: int) -> Reading:
        reading = self.try_read(code)
        if not isinstance(reading, Reading):
            raise ReadFailed(f"reading 0x{code:02X} failed: {reading}")
        return reading

    def plausible(self, code: int, reading: Reading | str) -> bool:
        if not isinstance(reading, Reading) or reading.not_available:
            return False
        if code in BITFIELDS:
            # A real 0xFD reply carries the capabilities mask as its max and no bit outside it; a
            # phantom answering with another code's data does not. All bits off (0) is a real state.
            mask = self.caps.mask(code) if self.caps else 0
            return mask != 0 and reading.max == mask and not reading.cur & ~mask
        values = self.caps.values(code) if self.caps else ()
        if values:
            return reading.cur in values
        return reading.cur <= reading.max

    def read_for_write(self, code: int) -> int:
        reading = self.read_one(code)
        if reading.not_available:
            raise ReadFailed(f"0x{code:02X} reads 0xFE/0xFE (not available in the current mode); not writing")
        if not self.plausible(code, reading):
            # A stale or garbled reply gets one more read before the write is abandoned.
            reading = self.read_one(code)
            if not self.plausible(code, reading):
                raise ReadFailed(
                    f"0x{code:02X} reads 0x{reading.cur:04X} (max 0x{reading.max:04X}), "
                    "which is not a plausible value; not writing"
                )
        self._fresh[code] = reading.cur
        return reading.cur

    def untested_reasons(self, feature: Feature, value: int | None = None) -> list[str]:
        display = self.need_display()
        reasons = []
        if (display.model.upper(), feature.name) not in VERIFIED:
            reasons.append(f"{feature.name} has not been verified on the {display.model}")
        if self.link is not None and self.link.transport != "DisplayPort":
            reasons.append(f"writes over {self.link.transport} have not been verified on any monitor")
        elif self.input_reason:
            reasons.append(self.input_reason)
        if value is not None and value > 0xFF:
            reasons.append("16-bit writes have not been verified on any monitor")
        return reasons

    def read_input(self, reading: Reading | str | None = None) -> None:
        if self.input_reason is not None or not self.profile.writes or self.write_block:
            return
        if self.link is None or self.link.transport != "DisplayPort":
            return
        if not self.need_caps().has(0x60):
            self.input_reason = "the capabilities do not list 0x60, so the monitor's own input is unknown"
            return
        if reading is None:
            reading = self.try_read(0x60)
        # display-toolkit reports 0x60 as unreliable while auto input detection is on, so a reply
        # that is not a listed DisplayPort input makes the write untested rather than blocking it.
        if not isinstance(reading, Reading) or not self.plausible(0x60, reading):
            self.input_reason = (
                f"the monitor's own input (0x60) read {describe_reading(reading)}, so its link is unknown"
            )
            return
        link = INPUT_LINKS.get(reading.cur)
        if link == "DisplayPort":
            self.input_reason = ""
            return
        name = link or f"input 0x{reading.cur:02X}"
        self.input_reason = (
            f"the monitor's own input is {name} (0x60 = 0x{reading.cur:02X}) on a DisplayPort connector, as "
            f"through an adapter or a dock, and writes over {name} have not been verified on any monitor"
        )

    def check_untested(self, feature: Feature, value: int | None = None) -> None:
        reasons = self.untested_reasons(feature, value)
        # identify() already refused an --untested that names another model.
        if reasons and self.untested is None:
            model = self.need_display().model
            raise Refused(
                f"{'; '.join(reasons)}. To try it anyway, re-run with --untested {model}, "
                "then please report what happened (see `osdless report`)"
            )

    def availability(self, feature: Feature) -> Availability:
        display = self.need_display()
        caps = self.need_caps()
        code = feature.code
        if not self.profile.writes:
            return Availability("unavailable", f"read-only: {self.reasons[-1]}", ())
        if self.link is None:
            return Availability("unavailable", self.unmapped(), ())
        if self.write_block:
            return Availability("unavailable", self.write_block, ())
        if feature.oled and not asus_oled(display.model):
            return Availability("unavailable", not_oled(display.model), ())
        if not caps.has(code):
            return Availability("unavailable", f"0x{code:02X} is not in this monitor's capabilities", ())
        if feature.write in BIT_WRITES:
            mask = caps.mask(code)
            if not feature.bit & mask:
                return Availability(
                    "unavailable", f"bit 0x{feature.bit:04X} is not in the capabilities mask 0x{mask:04X}", ()
                )
            values: tuple[str, ...] = ("on", "off") if feature.write is Write.BIT else ()
        else:
            values = tuple(word for word, value in feature.choices if value in caps.values(code))
            if not values:
                return Availability("unavailable", f"the capabilities list none of its values: {caps.listed(code)}", ())
        reasons = self.untested_reasons(feature)
        if reasons:
            return Availability("untested", "; ".join(reasons), values)
        return Availability("verified", f"verified on the {display.model}", values)

    def offer(self, feature: Feature, value: int | None = None, word: str = "") -> None:
        """Refuses a write this monitor does not offer, before the register it would change is read."""
        availability = self.availability(feature)
        if availability.status == "unavailable":
            raise Refused(f"{feature.name}: {availability.reason}")
        caps = self.need_caps()
        if value is not None and value not in caps.values(feature.code):
            raise Refused(
                f"{feature.name} {word} (0x{feature.code:02X} = {value:02X}) is not in this monitor's capabilities: "
                f"{caps.listed(feature.code)}"
            )
        # Twice, so that a write refused for its other reasons reads nothing first.
        self.check_untested(feature)
        self.read_input()
        self.check_untested(feature)

    def unmapped(self) -> str:
        if self.ddc_of is not None:
            return (
                f"/dev/i2c-{self.bus} is the `ddc` adapter of {self.ddc_of}, not its DP-AUX adapter (a passive "
                "DisplayPort-to-HDMI adapter does this), so osdless only reads this monitor"
            )
        return (
            f"/dev/i2c-{self.bus} is not the DDC/CI adapter of a connected DRM connector (NVIDIA's driver, "
            "MST hubs and docks do this), so osdless only reads this monitor"
        )

    def check_write(self, feature: Feature, old: int, value: int) -> None:
        code = feature.code
        if code in NEVER_WRITE:
            raise Refused(f"0x{code:02X} is never written by osdless")
        if not self.profile.writes:
            model = self.display.model if self.display else "monitor"
            raise Refused(f"this {model} is read-only for osdless: {self.reasons[-1]}")
        if feature not in self.profile.features:
            raise Refused(f"0x{code:02X} ({feature.name}) is not something osdless writes")
        if self.display is None:
            raise Refused("the monitor's identity has not been confirmed")
        if self.link is None:
            raise Refused(self.unmapped())
        if self.write_block:
            raise Refused(self.write_block)
        if self.link.transport == "DisplayPort" and self.input_reason is None:
            raise Refused("the monitor's own input (0x60) has not been read, so its link is unknown")
        if feature.oled and not asus_oled(self.display.model):
            raise Refused(f"{feature.name}: {not_oled(self.display.model)}")
        if self.caps is None or not self.caps.has(code):
            raise Refused(f"0x{code:02X} is not in the monitor's capabilities")
        if not 0 <= value <= 0xFFFF:
            raise Refused(f"0x{value:X} does not fit in the 16 bits ddcutil sends")
        if value > 0xFF and (feature.write not in BIT_WRITES or (old ^ value) & 0xFF00):
            raise Refused(
                f"0x{code:02X} 0x{old:04X} -> 0x{value:04X}: a value above 0xFF is only sent to carry 0xFD's "
                "high byte through unchanged"
            )
        if feature.write in BIT_WRITES:
            self.check_bit_write(feature, old, value)
        elif value not in dict(feature.choices).values():
            raise Refused(f"0x{value:02X} is not one of {feature.name}'s values")
        elif value not in self.caps.values(code):
            listed = " ".join(f"{v:02X}" for v in self.caps.values(code)) or "none"
            raise Refused(f"0x{value:02X} is not among 0x{code:02X}'s capability values ({listed})")
        self.check_untested(feature, value)

    def check_bit_write(self, feature: Feature, old: int, value: int) -> None:
        code = feature.code
        changed = old ^ value
        mask = self.caps.mask(code) if self.caps else 0
        if changed == 0 or changed & (changed - 1):
            raise Refused(f"0x{code:02X} 0x{old:04X} -> 0x{value:04X} does not change exactly one bit")
        if (old | changed) & ~mask:
            raise Refused(
                f"0x{code:02X} 0x{old:04X} -> 0x{value:04X} touches bits outside the capabilities mask 0x{mask:04X}"
            )
        refuse_while_cleaning(old)
        # Setting bit 0x10 starts a cleaning run, which only `oled clean` may do, behind its guards.
        if value & FD_PIXEL_CLEANING and feature.write is not Write.CLEAN:
            raise Refused("only `oled clean` may set 0xFD bit 0x0010 (pixel cleaning)")
        if changed != feature.bit:
            raise Refused(
                f"0x{code:02X} 0x{old:04X} -> 0x{value:04X} changes bit 0x{changed:04X}, "
                f"not {feature.name}'s bit 0x{feature.bit:04X}"
            )

    def journal(self, message: str) -> None:
        display = self.need_display()
        connector = self.link.connector if self.link else display.connector or "unmapped"
        syslog(f"{display.model} i2c-{display.bus} {connector}: {message}")

    def set_value(self, feature: Feature, value: int, what: str) -> int | None:
        """The only path to setvcp. Returns ddcutil's exit status, None on a timeout."""
        code = feature.code
        old = self._fresh.pop(code, None)
        if old is None:
            raise Refused(f"no fresh read of 0x{code:02X} to base this write on")
        self.check_write(feature, old, value)
        # Logged before sending: a run killed between setvcp and its read-back must still leave a trace.
        self.journal(f"0x{code:02X} {what}: sending old=0x{old:04X} new=0x{value:04X}")
        text = f"0x{value:02X}" if value <= 0xFF else f"0x{value:04X}"
        status, _ = self.ddc.run(
            DDCUTIL_WRITE, ["--bus", str(self.bus), "--noverify", f"0x{code:02X}", text], TIMEOUT_SET
        )
        return status

    def write_confirmed(
        self,
        feature: Feature,
        desired: Callable[[int], int],
        what: str,
        *,
        retries: int = 1,
        settle: float = SETTLE,
        precheck: Callable[[int], None] | None = None,
        prepare: Callable[[], None] | None = None,
    ) -> WriteResult:
        code = feature.code
        old = self.read_for_write(code)
        if precheck is not None:
            precheck(old)
        written = desired(old)
        if written == old:
            del self._fresh[code]
            return WriteResult(old, old, False)
        self.check_write(feature, old, written)
        for reason in self.untested_reasons(feature, written):
            warn(f"untested: {reason}; going ahead because of --untested, so please report what happened")
        if prepare is not None:
            prepare()
        for attempt in range(retries + 1):
            status = self.set_value(feature, written, what)
            time.sleep(settle)
            back = self.try_read(code)
            if not self.plausible(code, back):
                back = self.try_read(code)
            confirmed = isinstance(back, Reading) and self.plausible(code, back) and back.cur == written
            self.journal(
                f"0x{code:02X} {what}: old=0x{old:04X} new=0x{written:04X} readback={describe_reading(back)} "
                f"ddcutil={'timeout' if status is None else status} {'confirmed' if confirmed else 'NOT confirmed'}"
            )
            if confirmed:
                return WriteResult(old, written, True)
            if not isinstance(back, Reading) or not self.plausible(code, back):
                reason = "two read-backs failed or were implausible, so there is nothing safe to retry from"
                break
            # Anything but the old value means something else changed the register (or the reply is
            # another code's data), so a second write would apply changes nobody asked for.
            if back.cur != old:
                reason = "the read-back is neither the old nor the new value, so it is not written again"
                break
            if attempt == retries:
                reason = "the read-back still shows the old value"
                break
            self._fresh[code] = old
        raise WriteUnconfirmed(
            f"0x{code:02X} {what} not confirmed: old=0x{old:04X} new=0x{written:04X} "
            f"read back {describe_reading(back)}; {reason}"
        )


def refuse_while_cleaning(old: int) -> None:
    # Any 0xFD write while bit 0x10 reads set would carry the bit along, and if cleaning ends between
    # the read and the write, that write starts a new run.
    if old & FD_PIXEL_CLEANING:
        raise Refused(
            f"0xFD reads 0x{old:04X}: bit 0x0010 is set, so pixel cleaning is already running; try again when it ends"
        )


def describe_reading(reading: Reading | str) -> str:
    if isinstance(reading, Reading):
        return f"0x{reading.cur:04X}"
    return f"failed ({reading})"


def bit_states(entry: Code, value: int, mask: int) -> list[tuple[str, bool]]:
    names = dict(entry.names)
    bits = [1 << n for n in range(16) if mask & 1 << n]
    return [(names.get(bit, f"unidentified 0x{bit:04X}"), bool(value & bit)) for bit in bits]


def describe(entry: Code, r: Reading, caps: Caps) -> str:
    if r.not_available:
        return "n/a in this mode"
    names = dict(entry.names)
    unknown = f"unknown (0x{r.cur:04X})"
    match entry.kind:
        case Kind.LEVEL:
            text = f"{r.cur} / {r.max}"
        case Kind.NAMES:
            text = names.get(r.cur, unknown)
        case Kind.PRESET:
            text = names.get(r.sl, unknown)
        case Kind.GAMMA:
            # Some ASUS models list gamma in the high byte (72(5000 ...)); only the low-byte form is known here.
            text = f"{1 + r.cur / 100:.1f}" if r.cur <= 0xFF and r.cur in caps.values(entry.code) else unknown
        case Kind.SHORTCUTS:
            text = f"shortcut 1 {names.get(r.sl, f'0x{r.sl:02X}')}, shortcut 2 {names.get(r.sh, f'0x{r.sh:02X}')}"
        case Kind.BITS:
            parts = []
            for name, on in bit_states(entry, r.cur, caps.mask(entry.code)):
                if entry.code in BITFIELDS and name == "pixel cleaning running":
                    parts.append(name if on else "pixel cleaning idle")
                else:
                    parts.append(f"{name} {'on' if on else 'off'}")
            text = f"0x{r.cur:04X}: {', '.join(parts)}"
        case Kind.FIRMWARE:
            text = f"version {r.max}, panel 0x{r.sl:02X}"
        case Kind.VCP_TABLE:
            text = (
                f"0x{r.cur:02X}, VCP table {r.mh}.{r.ml} ({'new' if r.mh == 2 else 'old: E1/F8/F9 mean other things'})"
            )
        case _:
            text = f"0x{r.cur:04X} / 0x{r.max:04X}"
    return f"{text} ({entry.note})" if entry.note else text


# --- Pixel cleaning ----------------------------------------------------------------------------


def read_last_clean(path: Path) -> float | None:
    try:
        return float(json.loads(path.read_text())["started"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Refused(f"cannot read {path} ({exc}), so the 20 h guard cannot be checked; fix or remove it") from exc


def record_clean(path: Path, started: float) -> None:
    make_private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    stamp = datetime.fromtimestamp(started, UTC).isoformat(timespec="seconds")
    tmp.write_text(json.dumps({"started": started, "started_utc": stamp}) + "\n")
    os.replace(tmp, path)


def kde_inhibitions(command: str) -> Counter[str] | None:
    """Counts PowerDevil's requested inhibitions for `command` by type; None if PowerDevil cannot be asked."""
    busctl = shutil.which("busctl")
    if busctl is None:
        return None
    argv = [
        busctl,
        "--user",
        "--json=short",
        "get-property",
        "org.kde.Solid.PowerManagement",
        "/org/kde/Solid/PowerManagement/PolicyAgent",
        "org.kde.Solid.PowerManagement.PolicyAgent",
        "RequestedInhibitions",
    ]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False)
        if proc.returncode != 0:
            return None
        entries = json.loads(proc.stdout)["data"]
        # Entries are (type, "Running Script", command, mode, flags) for kde-inhibit.
        return Counter(e[0] for e in entries if isinstance(e, list) and len(e) > 2 and e[2] == command)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError):
        return None


def kde_inhibit_registered(command: str, before: Counter[str]) -> bool:
    deadline = time.monotonic() + KDE_REGISTER_WAIT
    while True:
        now = kde_inhibitions(command)
        if now is None:
            return False
        if all(now[kind] > before[kind] for kind in KDE_INHIBIT_TYPES):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(KDE_POLL)


def hold_inhibitors(seconds: int) -> list[tuple[str, subprocess.Popen[bytes]]]:
    """Starts every sleep/idle inhibitor available; returns the ones confirmed to hold."""
    hold = ["sleep", str(seconds)]
    candidates: list[tuple[str, list[str]]] = []
    kde = shutil.which("kde-inhibit")
    # kde-inhibit runs its command and exits 0 even when its D-Bus calls fail, so it only counts once
    # PowerDevil lists a new inhibition of each type for that command.
    kde_before = kde_inhibitions(hold[0]) if kde else None
    if kde and kde_before is not None:
        # --screenSaver keeps PowerDevil from turning the screen off, which would cut the signal.
        candidates.append(("kde-inhibit", [kde, "--power", "--screenSaver", *hold]))
    systemd = shutil.which("systemd-inhibit")
    if systemd:
        candidates.append(
            (
                "systemd-inhibit",
                [
                    systemd,
                    "--what=idle:sleep",
                    "--who=osdless",
                    "--why=OLED pixel cleaning",
                    "--mode=block",
                    "--no-ask-password",
                    *hold,
                ],
            )
        )
    started: list[tuple[str, subprocess.Popen[bytes]]] = []
    for name, argv in candidates:
        try:
            # Own session: if osdless is interrupted, the inhibitor still runs out its time.
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            continue
        started.append((name, proc))
        DETACHED.append(proc)
    time.sleep(INHIBIT_SETTLE)
    held = [(name, proc) for name, proc in started if proc.poll() is None]
    for name, proc in list(held):
        if name == "kde-inhibit" and kde_before is not None and not kde_inhibit_registered(hold[0], kde_before):
            warn("kde-inhibit is running, but PowerDevil does not list its inhibition, so it does not count")
            release_inhibitors([(name, proc)])
            held.remove((name, proc))
    return held


def release_inhibitors(holders: list[tuple[str, subprocess.Popen[bytes]]]) -> None:
    for _, proc in holders:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        if proc in DETACHED:
            DETACHED.remove(proc)


# --- Commands ----------------------------------------------------------------------------------


def display_json(mon: Monitor) -> dict[str, Any]:
    display = mon.need_display()
    return {
        "mfg": display.mfg,
        "model": display.model,
        "serial": display.serial,
        "bus": display.bus,
        "connector": mon.link.connector if mon.link else display.connector or mon.ddc_of or "",
        "transport": mon.link.transport if mon.link else None,
    }


def profile_json(mon: Monitor) -> dict[str, Any]:
    return {"name": mon.profile.name, "writes": mon.profile.writes, "reasons": list(mon.reasons)}


def location(link: Link | None, ddc_of: str | None) -> str:
    if link is not None:
        return f"{link.connector}, {link.transport}"
    if ddc_of is not None:
        return f"the `ddc` adapter of {ddc_of}"
    return "not mapped to a DRM connector"


def heading(mon: Monitor) -> list[str]:
    display = mon.need_display()
    where = location(mon.link, mon.ddc_of)
    lines = [f"{display.name()} on /dev/i2c-{display.bus} ({where}); capabilities {mon.caps_source}"]
    if mon.profile.writes:
        lines.append(f"profile {mon.profile.name}: writes the settings `osdless features` lists")
    else:
        lines.append(f"profile {mon.profile.name}: {mon.reasons[-1]}")
    return lines


def feature_json(feature: Feature, availability: Availability) -> dict[str, Any]:
    return {
        "command": feature.name,
        "code": f"0x{feature.code:02X}",
        "bit": f"0x{feature.bit:04X}" if feature.write in BIT_WRITES else None,
        "write": feature.write.value,
        "status": availability.status,
        "values": list(availability.values),
        "reason": availability.reason,
    }


def cmd_list(mon: Monitor, args: argparse.Namespace) -> int:
    drm = map_connectors()
    displays = detect_displays(mon.ddc)
    rows = []
    lines = []
    for display in displays:
        link = drm.links.get(display.bus)
        rows.append(
            {
                "mfg": display.mfg,
                "model": display.model,
                "serial": display.serial,
                "bus": display.bus,
                "connector": connector_of(display, drm),
                "transport": link.transport if link else None,
            }
        )
        where = location(link, None if link else drm.dp_ddc.get(display.bus))
        lines.append(f"/dev/i2c-{display.bus:<3} {display.name()} ({where}{'' if link else ': read-only'})")
    print(json.dumps({"displays": rows}, indent=2) if args.json else "\n".join(lines))
    return EXIT_OK


def cmd_status(mon: Monitor, args: argparse.Namespace) -> int:
    mon.connect()
    caps = mon.need_caps()
    entries = mon.profile.status
    readings = dict(mon.early)
    readings.update(mon.read([e.code for e in entries if caps.has(e.code) and e.code not in readings]))
    failed = [e.code for e in entries if caps.has(e.code) and not isinstance(readings.get(e.code), Reading)]
    ef = mon.early.get(0xEF)
    if args.json:
        codes: dict[str, Any] = {}
        for entry in entries:
            key = f"0x{entry.code:02X}"
            r = readings.get(entry.code)
            if not caps.has(entry.code):
                codes[key] = {"label": entry.label, "error": "not in capabilities"}
            elif not isinstance(r, Reading):
                codes[key] = {"label": entry.label, "error": r}
            else:
                item: dict[str, Any] = {
                    "label": entry.label,
                    "cur": r.cur,
                    "max": r.max,
                    "raw": {"mh": r.mh, "ml": r.ml, "sh": r.sh, "sl": r.sl},
                    "available": not r.not_available,
                    "text": describe(entry, r, caps),
                }
                if entry.kind is Kind.BITS and not r.not_available:
                    item["bits"] = dict(bit_states(entry, r.cur, caps.mask(entry.code)))
                codes[key] = item
        document = {
            "display": display_json(mon),
            "profile": profile_json(mon),
            "caps_source": mon.caps_source,
            "vcp_table": f"{ef.mh}.{ef.ml}" if asus_support_code(ef) else None,
            "codes": codes,
        }
        print(json.dumps(document, indent=2))
    else:
        print("\n".join(heading(mon)))
        for entry in entries:
            r = readings.get(entry.code)
            if not caps.has(entry.code):
                text = "not in capabilities"
            elif isinstance(r, Reading):
                text = describe(entry, r, caps)
            else:
                text = f"read failed: {r}"
            print(f"  0x{entry.code:02X}  {entry.label:<18} {text}")
    if failed:
        warn("could not read " + ", ".join(f"0x{code:02X}" for code in failed))
        return EXIT_READ_FAILED
    return EXIT_OK


def cmd_get(mon: Monitor, args: argparse.Namespace) -> int:
    code: int = args.vcp
    mon.connect()
    caps = mon.need_caps()
    r = mon.read_one(code)
    values = caps.values(code)
    if args.json:
        document = {
            "code": f"0x{code:02X}",
            "cur": r.cur,
            "max": r.max,
            "raw": {"mh": r.mh, "ml": r.ml, "sh": r.sh, "sl": r.sl},
            "available": not r.not_available,
            "caps_values": list(values),
        }
        print(json.dumps(document, indent=2))
        return EXIT_OK
    note = "  n/a in this mode" if r.not_available else ""
    if code in mon.profile.masked:
        listed = f"  caps mask 0x{caps.mask(code):04X}"
    else:
        listed = f"  caps ({' '.join(f'{v:02X}' for v in values)})" if values else ""
    print(
        f"0x{code:02X}: cur=0x{r.cur:04X} ({r.cur}) max=0x{r.max:04X} ({r.max}) "
        f"mh={r.mh:02X} ml={r.ml:02X} sh={r.sh:02X} sl={r.sl:02X}{listed}{note}"
    )
    return EXIT_OK


def cmd_features(mon: Monitor, args: argparse.Namespace) -> int:
    mon.connect()
    mon.read_input()
    rows = [(feature, mon.availability(feature)) for feature in FEATURES]
    if args.json:
        document = {
            "display": display_json(mon),
            "profile": profile_json(mon),
            "features": [feature_json(feature, availability) for feature, availability in rows],
        }
        print(json.dumps(document, indent=2))
        return EXIT_OK
    display = mon.need_display()
    print(heading(mon)[0])
    print(f"profile {mon.profile.name} ({'writes' if mon.profile.writes else 'read-only'}):")
    for reason in mon.reasons:
        print(f"  {reason}")
    print()
    for feature, availability in rows:
        where = (
            f"0x{feature.code:02X} bit 0x{feature.bit:04X}" if feature.write in BIT_WRITES else f"0x{feature.code:02X}"
        )
        values = "|".join(availability.values)
        if availability.status == "untested":
            state = f"needs --untested {display.model}: {availability.reason}"
        else:
            state = availability.reason if availability.status == "unavailable" else "ready"
        print(f"  {feature.name:<17} {where:<16} {values:<26} {state}")
    return EXIT_OK


def file_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def gpu_driver(link: Link | None) -> str | None:
    if link is None:
        return None
    try:
        return Path(os.readlink(drm_root() / link.card / "device" / "driver")).name
    except OSError:
        return None


def cmd_report(mon: Monitor, args: argparse.Namespace) -> int:
    output = Path(args.output)
    if not output.is_dir():
        raise Refused(f"{output} is not a directory")
    if not os.access(output, os.W_OK | os.X_OK):
        raise Refused(f"cannot write the report into {output}: permission denied")
    mon.connect()
    display = mon.need_display()
    caps = mon.need_caps()
    # Only codes the monitor lists: 0xFE times out, and 0xDD can answer with another code's data.
    codes = [code for code in sorted(caps.vcp) if code not in NEVER_SEND]
    readings: dict[int, Reading | str] = {}
    for start in range(0, len(codes), REPORT_BATCH):
        readings.update(mon.read(codes[start : start + REPORT_BATCH]))
    mon.read_input(readings.get(0x60))
    raw = caps.raw.replace(display.serial, REDACTED) if len(display.serial) >= 4 else caps.raw
    registers = {f"{code:02X}": [r.cur, r.max] for code, r in readings.items() if isinstance(r, Reading)}
    errors = {f"{code:02X}": r for code, r in readings.items() if not isinstance(r, Reading)}
    features = [feature_json(feature, mon.availability(feature)) for feature in FEATURES]
    system = {
        "kernel": os.uname().release,
        "gpu_driver": gpu_driver(mon.link),
        "transport": mon.link.transport if mon.link else None,
    }
    sha256 = file_sha256()
    ddcutil = mon.ddc.version()
    document = {
        "schema": REPORT_SCHEMA,
        "osdless": {"version": __version__, "sha256": sha256},
        "ddcutil": ddcutil,
        "system": system,
        "display": {**display_json(mon), "serial": REDACTED},
        "capabilities": raw,
        "registers": registers,
        "read_errors": errors,
        "profile": profile_json(mon),
        "features": features,
    }
    date = datetime.now().astimezone().date().isoformat()
    path = output / f"osdless-report-{safe_name(display.model)}-{date}.json"
    try:
        path.write_text(json.dumps(document, indent=2) + "\n")
    except OSError as exc:
        raise Refused(f"cannot write the report to {path}: {exc.strerror or exc}") from exc
    if args.json:
        print(json.dumps(document, indent=2))
        return EXIT_OK
    print(f"# osdless report: {display.mfg} {display.model}\n")
    print(f"- osdless {__version__}, sha256 {sha256[:16]}")
    print(f"- ddcutil {ddcutil}, kernel {system['kernel']}, GPU driver {system['gpu_driver'] or 'unknown'}")
    print(f"- connection: {mon.link.transport if mon.link else location(None, mon.ddc_of)}")
    print(f"- capabilities: MCCS {caps.mccs_version or 'unknown'}, {len(caps.vcp)} codes")
    print(f"- read: {len(registers)} codes, {len(errors)} failed")
    print(f"- profile: {mon.profile.name} ({'writes' if mon.profile.writes else 'read-only'})")
    for reason in mon.reasons:
        print(f"  - {reason}")
    print("\n| Feature | Status | Values | Why |\n|---|---|---|---|")
    for item in features:
        print(f"| `{item['command']}` | {item['status']} | {' '.join(item['values'])} | {item['reason']} |")
    print(f"\nWrote {path}. The serial number is redacted; attach the file to a monitor report.")
    return EXIT_OK


def print_result(feature: Feature, what: str, result: WriteResult, as_json: bool) -> int:
    code = feature.code
    if as_json:
        document = {
            "command": what,
            "code": f"0x{code:02X}",
            "old": result.old,
            "new": result.new,
            "written": result.changed,
        }
        print(json.dumps(document, indent=2))
    elif result.changed:
        print(f"0x{code:02X} {what}: 0x{result.old:04X} -> 0x{result.new:04X}, confirmed")
    else:
        print(f"0x{code:02X} {what}: already so (0x{result.old:04X}), nothing written")
    return EXIT_OK


def cmd_bit(mon: Monitor, args: argparse.Namespace) -> int:
    feature: Feature = args.feature
    bit = feature.bit
    on = args.state == "on"
    what = f"{feature.name} {args.state}"
    mon.connect()
    mon.offer(feature)
    result = mon.write_confirmed(
        feature, lambda cur: cur | bit if on else cur & ~bit, what, precheck=refuse_while_cleaning
    )
    return print_result(feature, what, result, args.json)


def cmd_value(mon: Monitor, args: argparse.Namespace) -> int:
    feature: Feature = args.feature
    value = dict(feature.choices)[args.choice]
    what = f"{feature.name} {args.choice}"
    mon.connect()
    mon.offer(feature, value, args.choice)
    if feature.write is not Write.SWITCH:
        result = mon.write_confirmed(feature, lambda _cur: value, what)
        return print_result(feature, what, result, args.json)
    try:
        result = mon.write_confirmed(feature, lambda _cur: value, what, retries=0, settle=SWITCH_SETTLE)
    except WriteUnconfirmed as exc:
        raise WriteUnconfirmed(
            f"{exc}. The switch may still be in progress, so it is not sent again; check with `osdless status`"
        ) from None
    return print_result(feature, what, result, args.json)


def cmd_clean(mon: Monitor, args: argparse.Namespace) -> int:
    state = clean_state_file()
    last = read_last_clean(state)
    now = time.time()
    if last is not None and now - last < CLEAN_GUARD:
        hours = (now - last) / 3600
        raise Refused(
            f"the last pixel cleaning started {hours:.1f} h ago; osdless waits 20 h between runs, "
            f"on any monitor (state: {state})"
        )
    mon.connect()
    mon.offer(CLEAN)
    power = mon.read_one(0xD6)
    if power.cur != 1:
        raise Refused(f"0xD6 power mode reads {power.cur}, not 1 (on); cleaning needs a live picture")
    # Past the end of --wait, with or without it: how long a run takes is only known from other
    # people's models, and no model is known to keep 0xFD bit 0x10 set until its run ends.
    hold_seconds = int(WAIT_CAP) + 60
    holders: list[tuple[str, subprocess.Popen[bytes]]] = []

    def prepare() -> None:
        holders.extend(hold_inhibitors(hold_seconds))
        if not holders:
            raise Refused("could not hold kde-inhibit or systemd-inhibit, and a sleeping PC would cut the signal")
        try:
            record_clean(state, time.time())
        except OSError as exc:
            release_inhibitors(holders)
            raise Refused(f"cannot record this run in {state} ({exc}), so the 20 h guard would not hold") from exc

    result = mon.write_confirmed(
        CLEAN,
        lambda cur: cur | FD_PIXEL_CLEANING,
        CLEAN.name,
        retries=0,
        precheck=refuse_while_cleaning,
        prepare=prepare,
    )
    names = [name for name, _ in holders]
    started = f"0xFD oled clean: 0x{result.old:04X} -> 0x{result.new:04X}, pixel cleaning started"
    document: dict[str, Any] = {
        "command": CLEAN.name,
        "code": f"0x{CLEAN.code:02X}",
        "old": result.old,
        "new": result.new,
        "written": result.changed,
        "inhibitors": names,
        "hold_seconds": hold_seconds,
    }
    if not args.json:
        print(
            f"{started}; the screen is unavailable for several minutes. "
            f"Holding {', '.join(names)} for {hold_seconds // 60} min."
        )
    if args.wait:
        minutes = wait_for_clean(mon, result.old, started)
        document["finished_after_min"] = round(minutes, 1)
        if not args.json:
            print(
                f"pixel cleaning finished after {minutes:.1f} min (0xFD 0x{result.old:04X}); "
                "the inhibitors run out on their own"
            )
    if args.json:
        print(json.dumps(document, indent=2))
    return EXIT_OK


def wait_for_clean(mon: Monitor, before: int, started: str) -> float:
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= WAIT_CAP:
            raise ReadFailed(
                f"{started}, but it was not seen finishing within {WAIT_CAP / 60:.0f} min; "
                "the inhibitors run out on their own"
            )
        time.sleep(min(POLL_INTERVAL, WAIT_CAP - elapsed))
        reading = mon.try_read(CLEAN.code)
        elapsed = time.monotonic() - start
        # Only the exact value from before the run counts as its end, not a phantom or garbled reply
        # that happens to have bit 0x10 clear.
        if isinstance(reading, Reading) and mon.plausible(CLEAN.code, reading) and reading.cur == before:
            return elapsed / 60
        warn(
            f"still cleaning after {elapsed / 60:.1f} min "
            f"(0xFD {describe_reading(reading)}, waiting for 0x{before:04X})"
        )


# --- Command line ------------------------------------------------------------------------------

Handler = Callable[[Monitor, argparse.Namespace], int]
GLOBAL_DEFAULTS: dict[str, Any] = {
    "serial": None,
    "model": None,
    "connector": None,
    "bus": None,
    "json": False,
    "untested": None,
}


class Parser(argparse.ArgumentParser):
    # argparse would exit on a bad command line, before --json could print its one document.
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise Refused(f"{message} (see `{self.prog} --help`)")


def build_parser() -> argparse.ArgumentParser:
    # Defined once and given to every parser with SUPPRESS defaults, so these options work before or
    # after the command without a subparser's default overwriting what was given earlier.
    common = Parser(add_help=False)
    common.add_argument("--serial", default=argparse.SUPPRESS, help="the monitor with this EDID serial number")
    common.add_argument("--model", default=argparse.SUPPRESS, help="the monitor with this EDID model, e.g. PG39WCDM")
    common.add_argument("--connector", default=argparse.SUPPRESS, help="DRM connector, e.g. card1-DP-1 or DP-1")
    common.add_argument("--bus", type=int, default=argparse.SUPPRESS, help="I2C bus N, as in /dev/i2c-N")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print one JSON document")
    common.add_argument(
        "--untested",
        metavar="MODEL",
        default=argparse.SUPPRESS,
        help="allow a write not yet verified on this model; MODEL must name the chosen monitor",
    )
    parser = Parser(
        prog="osdless",
        description="Change ASUS gaming monitor settings without the OSD, and read any DDC/CI monitor (ddcutil).",
        epilog="Exit codes: 0 ok, 2 refused, 3 read failed, 4 write not confirmed.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"osdless {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    reads: list[tuple[str, str, Handler]] = [
        ("list", "list the DDC/CI displays ddcutil detects", cmd_list),
        ("status", "read and decode the monitor's state", cmd_status),
        ("get", "raw read of one VCP code from the capabilities", cmd_get),
        ("features", "what osdless can write on this monitor, and why not", cmd_features),
        ("report", "write a report for adding or verifying a monitor (serial redacted)", cmd_report),
    ]
    for name, summary, handler in reads:
        command = sub.add_parser(name, help=f"{summary} (read-only)", parents=[common])
        command.set_defaults(handler=handler, writes=False)
        if name == "get":
            command.add_argument("code", help="hex VCP code, e.g. FD or 0xFD")
        elif name == "report":
            command.add_argument("--output", default=".", help="directory for the report file (default: here)")

    groups: dict[str, argparse._SubParsersAction[Parser]] = {}
    for feature in FEATURES:
        *path, leaf = feature.command
        container = sub
        for word in path:
            if word not in groups:
                group = sub.add_parser(word, help=GROUP_HELP[word], parents=[common])
                groups[word] = group.add_subparsers(dest=f"{word}_command", required=True, metavar="SETTING")
            container = groups[word]
        command = container.add_parser(leaf, help=feature.summary, parents=[common])
        if feature.write is Write.BIT:
            command.add_argument("state", choices=("on", "off"))
            command.set_defaults(handler=cmd_bit)
        elif feature.write is Write.CLEAN:
            command.add_argument("--yes", action="store_true", help="confirm that the screen may go dark")
            command.add_argument("--wait", action="store_true", help="poll until cleaning finishes (10 min cap)")
            command.set_defaults(handler=cmd_clean)
        else:
            command.add_argument("choice", choices=tuple(word for word, _ in feature.choices))
            command.set_defaults(handler=cmd_value)
            if feature.write is Write.SWITCH:
                command.add_argument("--yes", action="store_true", help="confirm the switch")
        command.set_defaults(feature=feature, writes=True)
    return parser


def preflight(args: argparse.Namespace) -> None:
    """Policy checks that need no lock and no DDC traffic."""
    feature: Feature | None = getattr(args, "feature", None)
    if feature is not None and feature.warning:
        warn(feature.warning)
        if not args.yes:
            raise Refused("re-run with --yes to go ahead")
    if args.command == "get":
        args.vcp = parse_code(args.code)
        if args.vcp in NEVER_SEND:
            raise Refused(f"0x{args.vcp:02X} is on the never-send list")


def failed(exc: Failure, as_json: bool) -> int:
    warn(str(exc))
    if as_json:
        print(json.dumps({"ok": False, "exit": exc.exit_code, "error": str(exc)}))
    return exc.exit_code


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    try:
        args = build_parser().parse_args(words)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_REFUSED
    except Refused as exc:
        return failed(exc, "--json" in words)
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    selector = Selector(args.serial, args.model, args.connector, args.bus)
    lock = None
    try:
        preflight(args)
        lock = acquire_lock()
        handler: Handler = args.handler
        return handler(Monitor(Ddcutil(writes=args.writes), selector, args.untested), args)
    except Failure as exc:
        return failed(exc, args.json)
    except KeyboardInterrupt:
        warn("interrupted")
        return EXIT_INTERRUPTED
    finally:
        if lock is not None:
            os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
