"""Replays a monitor from tests/fixtures through a fake ddcutil, fake helpers and a fake DRM tree.

Nothing here talks to a real monitor, the journal or the desktop: PATH holds only the fakes.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import osdless

HERE = Path(__file__).resolve().parent
TOOL = HERE.parent / "osdless.py"
FIXTURES = HERE / "fixtures"
FAKE_DDCUTIL = HERE / "fake_ddcutil.py"

HELPER = """
import json, os, sys, time
with open(os.environ["FAKE_HELPER_LOG"], "a") as f:
    f.write(json.dumps({"tool": NAME, "argv": sys.argv[1:], "pid": os.getpid(), "t": time.time()}) + "\\n")
if NAME != "logger":
    if os.environ.get("FAKE_INHIBIT_FAIL") in (NAME, "all"):
        sys.exit(1)
    time.sleep(float(os.environ.get("FAKE_INHIBIT_SLEEP", "3")))
"""

# PowerDevil's RequestedInhibitions as `busctl --user --json=short get-property` prints it, with the
# entry shapes and registration delay seen on Plasma 6. FAKE_BUSCTL: "register" lists live fake
# kde-inhibit runs after FAKE_KDE_REGISTER_DELAY seconds, "absent" never lists them (a kde-inhibit
# whose D-Bus calls failed), "power-only" lists only the --power half (no screen-saver service),
# "fail" is an unreachable bus.
BUSCTL = """
import json, os, sys, time
log = os.environ["FAKE_HELPER_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"tool": "busctl", "argv": sys.argv[1:], "pid": os.getpid(), "t": time.time()}) + "\\n")
mode = os.environ.get("FAKE_BUSCTL", "register")
if mode == "fail":
    print("Failed to connect to user scope bus via local transport: No such file or directory", file=sys.stderr)
    sys.exit(1)
data = [["sleep", "/usr/bin/example-player", "Playing a video", "block", 3]]
if os.environ.get("FAKE_BUSCTL_PREEXISTING"):
    data += [["sleep", "Running Script", "sleep", "block", 3], ["idle", "Running Script", "sleep", "block", 3]]
if mode in ("register", "power-only"):
    delay = float(os.environ.get("FAKE_KDE_REGISTER_DELAY", "0.2"))
    for entry in map(json.loads, open(log).read().splitlines()):
        if entry["tool"] != "kde-inhibit" or time.time() - entry["t"] < delay:
            continue
        try:
            os.kill(entry["pid"], 0)
        except ProcessLookupError:
            continue
        command = next(arg for arg in entry["argv"] if not arg.startswith("--"))
        if "--power" in entry["argv"]:
            data.append(["sleep", "Running Script", command, "block", 3])
        if "--screenSaver" in entry["argv"] and mode == "register":
            data.append(["idle", "Running Script", command, "block", 3])
print(json.dumps({"type": "a(ssssu)", "data": data}))
"""

FAST = {
    "POLL_INTERVAL": 0.05,
    "WAIT_CAP": 1.5,
    "INHIBIT_SETTLE": 0.1,
    "KDE_POLL": 0.05,
    "KDE_REGISTER_WAIT": 1.0,
    "SWITCH_SETTLE": 0.3,
}
REAL = {name: getattr(osdless, name) for name in FAST}


def fixture_names() -> list[str]:
    return sorted(path.stem for path in FIXTURES.glob("*.json"))


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def caps_with(caps: str, old: str, new: str) -> str:
    assert caps.count(old) == 1, old
    return caps.replace(old, new)


class Rig:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.bin = root / "bin"
        self.bin.mkdir()
        self._script(self.bin / "ddcutil", FAKE_DDCUTIL.read_text())
        for name in ("logger", "kde-inhibit", "systemd-inhibit"):
            self._script(self.bin / name, f"NAME = {name!r}\n{HELPER}")
        self._script(self.bin / "busctl", BUSCTL)
        self.drm = root / "drm"
        self.runtime = root / "run"
        self.cache = root / "cache"
        self.statehome = root / "state"
        self.runtime.mkdir()
        self.state_path = root / "fake-state.json"
        self.helper_log = root / "helpers.jsonl"
        self.helper_log.touch()
        for name in (
            "OSDLESS_NO_SYSLOG",
            "OSDLESS_REAL_MONITOR",
            "FAKE_INHIBIT_FAIL",
            "FAKE_BUSCTL",
            "FAKE_BUSCTL_PREEXISTING",
            "FAKE_KDE_REGISTER_DELAY",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in {
            "PATH": str(self.bin),
            "HOME": str(root / "home"),
            "OSDLESS_DDCUTIL": str(self.bin / "ddcutil"),
            "OSDLESS_DRM_ROOT": str(self.drm),
            "XDG_RUNTIME_DIR": str(self.runtime),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_STATE_HOME": str(self.statehome),
            "FAKE_DDC_STATE": str(self.state_path),
            "FAKE_HELPER_LOG": str(self.helper_log),
            "FAKE_INHIBIT_SLEEP": "3",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/osdless-test-bus",
        }.items():
            monkeypatch.setenv(name, value)
        for name, seconds in FAST.items():
            monkeypatch.setattr(osdless, name, seconds)
        self.use("PG39WCDM")

    def _script(self, path: Path, body: str) -> None:
        # The kernel cuts a shebang line at 255 bytes, so a long interpreter path goes in the body of
        # a shell wrapper instead.
        source = self.root / "fakes" / f"{path.name}.py"
        source.parent.mkdir(exist_ok=True)
        source.write_text(body)
        path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -IB {shlex.quote(str(source))} "$@"\n')
        path.chmod(0o755)

    def use(self, fixture: str | dict[str, Any], *, connector: str | None = None, bus: int | None = None) -> None:
        """Attaches the fixture's monitor, optionally on another connector or bus."""
        data = load_fixture(fixture) if isinstance(fixture, str) else copy.deepcopy(fixture)
        display = data["display"]
        self.fixture = data
        self.serial = display["serial"]
        self.model = display["model"]
        self.bus = bus if bus is not None else display["bus"]
        self.connector = connector or display["connector"]
        regs = copy.deepcopy(data["registers"])
        if display["mfg"] == "AUS":
            # 0xDD answers like the real phantom does, so a read of it would succeed and only the
            # violation list catches it.
            regs.setdefault("DD", [0x0202, 0xFFFF])
        state = {
            "bus": self.bus,
            "displays": [
                {
                    "bus": self.bus,
                    "connector": self.connector,
                    "monitor": f"{display['mfg']}:{display['model']}:{display['serial']}",
                }
            ],
            "caps": data["capabilities"],
            "regs": regs,
            "read_errors": data.get("read_errors", {}),
            "fail_reads": {},
            "fail_read_numbers": {},
            "ignore_writes": {},
            "reads": {},
            "writes": [],
            "write_args": [],
            "violations": [],
        }
        self.state_path.write_text(json.dumps(state))
        self.build_drm()

    def build_drm(self) -> None:
        if self.drm.exists():
            for path in sorted(self.drm.rglob("*"), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
        (self.drm / "i2c-4").mkdir(parents=True)
        (self.drm / "drivers" / "amdgpu").mkdir(parents=True)
        (self.drm / "card1" / "device").mkdir(parents=True)
        (self.drm / "card1" / "device" / "driver").symlink_to("../../drivers/amdgpu")
        self.add_connector(self.connector, "connected", self.bus)
        if self.connector != "card1-DP-2":
            self.add_connector("card1-DP-2", "disconnected", 10)

    def add_connector(self, name: str, status: str, bus: int) -> None:
        path = self.drm / name
        path.mkdir()
        (path / "status").write_text(status + "\n")
        if "-DP-" in name:
            (path / f"i2c-{bus}").mkdir()
            (path / "ddc").symlink_to("../i2c-4")
        else:
            (self.drm / f"i2c-{bus}").mkdir(exist_ok=True)
            (path / "ddc").symlink_to(f"../i2c-{bus}")

    def finish(self) -> None:
        for proc in list(osdless.DETACHED):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
            proc.wait()
        osdless.DETACHED.clear()
        assert self.state()["violations"] == [], "osdless sent something it must never send"

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = osdless.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_process(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(TOOL), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.state_path.read_text())
        return data

    def mutate(self, change: Callable[[dict[str, Any]], object]) -> None:
        state = self.state()
        change(state)
        self.state_path.write_text(json.dumps(state))

    def set_reg(self, code: str, cur: int, mx: int | None = None) -> None:
        def change(state: dict[str, Any]) -> None:
            reg = state["regs"][code]
            reg[0] = cur
            if mx is not None:
                reg[1] = mx

        self.mutate(change)

    def set_state(self, **values: Any) -> None:
        self.mutate(lambda state: state.update(values))

    def set_caps(self, old: str, new: str) -> None:
        caps = caps_with(self.state()["caps"], old, new)
        self.set_state(caps=caps)

    def writes(self) -> list[tuple[int, int]]:
        return [(code, value) for code, value in self.state()["writes"]]

    def calls(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        path = Path(str(self.state_path) + ".calls")
        if not path.exists():
            return []
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            merged.setdefault(entry["id"], {}).update(entry)
        return sorted(merged.values(), key=lambda c: c["start"])

    def forget_calls(self) -> None:
        Path(str(self.state_path) + ".calls").unlink(missing_ok=True)

    def subcommands(self) -> list[str]:
        return [call["command"] for call in self.calls()]

    def helper_calls(self, tool: str) -> list[list[str]]:
        lines = self.helper_log.read_text().splitlines()
        return [entry["argv"] for entry in map(json.loads, lines) if entry["tool"] == tool]

    def last_clean(self) -> Path:
        return self.statehome / "osdless" / "last-clean.json"
