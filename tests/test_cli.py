import contextlib
import fcntl
import io
import json
import subprocess
import sys
import time

import pytest

import osdless
from rig import TOOL

SENDING = ["-t", "osdless", "--", "PG39WCDM i2c-9 card1-DP-1: 0xFD oled uniform on: sending old=0x0029 new=0x0069"]


def test_one_run_at_a_time(rig):
    with open(rig.runtime / "osdless.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for argv in (("status",), ("oled", "uniform", "on"), ("get", "FD"), ("list",)):
            code, _, err = rig.run_cli(*argv)
            assert code == osdless.EXIT_REFUSED
            assert "another osdless run" in err
        assert rig.calls() == []
    assert rig.run_cli("status")[0] == osdless.EXIT_OK


def test_lock_is_released_after_a_failure(rig):
    rig.set_state(fail_reads={"FF": 1})
    assert rig.run_cli("status")[0] == osdless.EXIT_READ_FAILED
    with open(rig.runtime / "osdless.lock", "w") as probe:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_writes_are_logged(rig):
    rig.run_cli("oled", "uniform", "on")
    assert rig.helper_calls("logger") == [
        SENDING,
        [
            "-t",
            "osdless",
            "--",
            "PG39WCDM i2c-9 card1-DP-1: 0xFD oled uniform on: "
            "old=0x0029 new=0x0069 readback=0x0069 ddcutil=0 confirmed",
        ],
    ]


def test_unconfirmed_writes_are_logged(rig):
    rig.set_state(ignore_writes={"FD": -1})
    rig.run_cli("oled", "uniform", "on")
    logged = [argv[-1] for argv in rig.helper_calls("logger")]
    assert len(logged) == 4
    assert logged[0::2] == [SENDING[-1]] * 2
    assert all(line.endswith("NOT confirmed") for line in logged[1::2])


def test_write_is_logged_before_it_is_sent(rig, monkeypatch):
    real_sleep = time.sleep

    def interrupted_settle(seconds):
        if seconds == osdless.SETTLE:
            raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(osdless.time, "sleep", interrupted_settle)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_INTERRUPTED
    assert "interrupted" in err
    assert rig.writes() == [(0xFD, 0x69)]
    assert rig.helper_calls("logger") == [SENDING]
    logged_at = next(json.loads(line)["t"] for line in rig.helper_log.read_text().splitlines())
    sent_at = next(c["start"] for c in rig.calls() if c["command"] == "setvcp")
    assert logged_at < sent_at


def test_write_killed_mid_setvcp_is_in_the_journal(rig):
    # timeout(1), a systemd stop or a script's command timeout all send SIGTERM.
    rig.set_state(setvcp_delay=1.5)
    proc = subprocess.Popen(
        [sys.executable, "-I", "-B", str(TOOL), "oled", "uniform", "on"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while "setvcp" not in rig.subcommands() and time.monotonic() < deadline:
        time.sleep(0.02)
    proc.terminate()
    assert proc.wait(timeout=10) == -15
    # The orphaned fake ddcutil still applies the write, then dies on its closed stdout.
    while not rig.writes() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert rig.writes() == [(0xFD, 0x69)]
    assert rig.helper_calls("logger") == [SENDING]


def test_reads_are_not_logged(rig, tmp_path):
    rig.run_cli("status")
    rig.run_cli("features")
    rig.run_cli("report", "--output", str(tmp_path))
    rig.run_cli("oled", "saver", "on", "--untested", "PG39WCDM")
    assert rig.helper_calls("logger") == []


def test_logging_can_be_skipped(rig, monkeypatch):
    monkeypatch.setenv("OSDLESS_NO_SYSLOG", "1")
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_OK
    assert rig.helper_calls("logger") == []


def test_missing_logger_warns_but_writes(rig):
    (rig.bin / "logger").unlink()
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_OK
    assert "not in the journal" in err


def test_slow_read_times_out_as_a_read_failure(rig, monkeypatch):
    rig.set_state(getvcp_delay=2)
    monkeypatch.setattr(osdless, "TIMEOUT_GET_BASE", 0.4)
    monkeypatch.setattr(osdless, "TIMEOUT_GET_PER_CODE", 0)
    start = time.monotonic()
    assert rig.run_cli("status")[0] == osdless.EXIT_READ_FAILED
    assert time.monotonic() - start < 1.8
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_READ_FAILED
    assert rig.writes() == []


@pytest.mark.parametrize(
    ("knob", "constant", "message"),
    [("detect_delay", "TIMEOUT_DETECT", "detect timed out"), ("caps_delay", "TIMEOUT_CAPS", "capabilities timed out")],
)
def test_slow_detect_and_capabilities_time_out_as_read_failures(rig, monkeypatch, knob, constant, message):
    monkeypatch.setattr(osdless, constant, 0.4)
    rig.set_state(**{knob: 3})
    start = time.monotonic()
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_READ_FAILED
    assert time.monotonic() - start < 2.5
    assert message in err
    rig.set_state(**{knob: 0})
    assert rig.writes() == []


def test_slow_write_is_not_confirmed(rig, monkeypatch):
    rig.set_state(setvcp_delay=2)
    monkeypatch.setattr(osdless, "TIMEOUT_SET", 0.4)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert "read back 0x0029" in err
    assert rig.subcommands().count("setvcp") == 2


def test_exit_codes_through_the_script(rig):
    assert rig.run_process("status").returncode == 0
    assert rig.run_process("get", "FE").returncode == 2
    rig.set_state(fail_reads={"FD": 1})
    assert rig.run_process("oled", "uniform", "on").returncode == 3
    rig.set_state(ignore_writes={"FD": -1})
    assert rig.run_process("oled", "uniform", "on").returncode == 4
    assert rig.run_process("--help").returncode == 0


def test_version(rig):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert osdless.main(["--version"]) == osdless.EXIT_OK
    assert out.getvalue() == f"osdless {osdless.__version__}\n"
    assert rig.calls() == []


def test_json_failures_print_one_document(rig):
    code, out, err = rig.run_cli("get", "FE", "--json")
    assert code == osdless.EXIT_REFUSED
    assert json.loads(out) == {"ok": False, "exit": 2, "error": "0xFE is on the never-send list"}
    assert "never-send" in err
    rig.set_state(fail_reads={"FD": 1})
    code, out, _ = rig.run_cli("--json", "oled", "uniform", "on")
    assert code == osdless.EXIT_READ_FAILED
    assert json.loads(out)["exit"] == 3


def test_global_options_work_before_or_after_the_command(rig):
    for argv in (("--json", "status"), ("status", "--json"), ("--serial", "SYNTHETIC0001", "--json", "get", "FD")):
        code, out, _ = rig.run_cli(*argv)
        assert code == osdless.EXIT_OK
        json.loads(out)
    assert rig.run_cli("--serial", "SYNTHETIC9999", "status")[0] == osdless.EXIT_READ_FAILED
    assert rig.run_cli("oled", "--untested", "PG39WCDM", "saver", "off")[0] == osdless.EXIT_OK


def test_a_bad_command_line_with_json_still_prints_one_document(rig):
    for argv in (("--json", "oled", "uniform", "maybe"), ("--json", "nosuchcommand"), ("oled", "uniform", "--json")):
        code, out, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_REFUSED, argv
        document = json.loads(out)
        assert document["ok"] is False and document["exit"] == 2
        assert "usage: osdless" in err
    assert "invalid choice: 'maybe'" in json.loads(rig.run_cli("--json", "oled", "uniform", "maybe")[1])["error"]
    code, out, err = rig.run_cli("oled", "uniform", "maybe")
    assert code == osdless.EXIT_REFUSED
    assert out == ""
    assert "invalid choice: 'maybe'" in err
    assert rig.calls() == []
