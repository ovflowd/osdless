import copy
import json
import os
import stat
import time

import osdless
from rig import REAL

CLEAN = ("oled", "clean", "--yes", "--untested", "PG39WCDM")


def test_clean_needs_yes(rig):
    code, _, err = rig.run_cli("oled", "clean", "--untested", "PG39WCDM")
    assert code == osdless.EXIT_REFUSED
    assert "about 6 to 8 minutes" in err
    assert rig.calls() == []


def test_clean_needs_untested(rig):
    code, _, err = rig.run_cli("oled", "clean", "--yes")
    assert code == osdless.EXIT_REFUSED
    assert "oled clean has not been verified on the PG39WCDM" in err
    assert rig.writes() == []
    assert rig.helper_calls("systemd-inhibit") == []
    assert not rig.last_clean().exists()


def test_clean_sets_the_bit_and_holds_inhibitors_past_the_wait_cap(rig, monkeypatch):
    monkeypatch.setattr(osdless, "WAIT_CAP", REAL["WAIT_CAP"])
    before = time.time()
    code, out, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_OK, err
    assert "about 6 to 8 minutes" in err
    assert "pixel cleaning started" in out
    assert "Holding kde-inhibit, systemd-inhibit for 11 min" in out
    assert rig.writes() == [(0xFD, 0x39)]
    assert rig.helper_calls("kde-inhibit") == [["--power", "--screenSaver", "sleep", "660"]]
    systemd = rig.helper_calls("systemd-inhibit")
    assert len(systemd) == 1
    assert "--what=idle:sleep" in systemd[0]
    assert "--who=osdless" in systemd[0]
    assert systemd[0][-2:] == ["sleep", "660"]
    started = json.loads(rig.last_clean().read_text())["started"]
    assert started >= before
    assert stat.S_IMODE(rig.last_clean().parent.stat().st_mode) == 0o700
    assert all(p.poll() is None for p in osdless.DETACHED), "inhibitors must outlive a run without --wait"


def test_clean_reads_the_power_mode_first(rig):
    rig.run_cli(*CLEAN)
    reads = [c["argv"][-1] for c in rig.calls() if c["command"] == "getvcp"]
    assert reads.index("0xD6") < reads.index("0xFD")


def test_clean_is_refused_within_20_hours(rig):
    rig.last_clean().parent.mkdir(parents=True)
    rig.last_clean().write_text(json.dumps({"started": time.time() - 19.9 * 3600}))
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "20 h between runs, on any monitor" in err
    assert rig.calls() == []


def test_clean_is_allowed_after_20_hours(rig):
    rig.last_clean().parent.mkdir(parents=True)
    rig.last_clean().write_text(json.dumps({"started": time.time() - 20.1 * 3600}))
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xFD, 0x39)]


def test_second_clean_right_after_the_first_is_refused(rig):
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_OK
    rig.set_reg("FD", 0x29)
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_REFUSED
    assert rig.writes() == [(0xFD, 0x39)]


def test_unreadable_state_file_refuses(rig):
    rig.last_clean().parent.mkdir(parents=True)
    rig.last_clean().write_text("garbage")
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_REFUSED
    assert rig.calls() == []


def test_clean_is_refused_unless_the_display_is_on(rig):
    rig.set_reg("D6", 5)
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "0xD6" in err
    assert rig.writes() == []
    assert rig.helper_calls("systemd-inhibit") == []
    assert not rig.last_clean().exists()


def test_clean_is_refused_while_cleaning_runs(rig):
    rig.set_reg("FD", 0x39)
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "already running" in err
    assert rig.writes() == []
    assert rig.helper_calls("systemd-inhibit") == []


def test_clean_failed_read_aborts(rig):
    rig.set_state(fail_reads={"FD": 1})
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_READ_FAILED
    assert rig.writes() == []
    assert not rig.last_clean().exists()


def test_clean_is_refused_without_an_inhibitor(rig, monkeypatch):
    monkeypatch.setenv("FAKE_INHIBIT_FAIL", "all")
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "inhibit" in err
    assert rig.writes() == []
    assert not rig.last_clean().exists()


def test_clean_is_refused_if_the_run_cannot_be_recorded(rig):
    rig.last_clean().parent.mkdir(parents=True)
    rig.last_clean().parent.chmod(0o500)
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "20 h guard would not hold" in err
    assert rig.writes() == []
    assert osdless.DETACHED == [], "the inhibitors are released again"


def test_clean_without_powerdevil_on_the_bus_uses_systemd_inhibit(rig, monkeypatch):
    monkeypatch.setenv("FAKE_BUSCTL", "fail")
    code, out, _ = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_OK
    assert rig.helper_calls("kde-inhibit") == []
    assert len(rig.helper_calls("systemd-inhibit")) == 1
    assert "Holding systemd-inhibit for" in out


def test_kde_inhibit_counts_only_once_powerdevil_lists_it(rig, monkeypatch):
    # The real kde-inhibit runs its command and exits 0 even when its D-Bus calls fail.
    monkeypatch.setenv("FAKE_INHIBIT_FAIL", "systemd-inhibit")
    monkeypatch.setenv("FAKE_INHIBIT_SLEEP", "30")
    for mode, preexisting in (("absent", ""), ("absent", "1"), ("power-only", "")):
        monkeypatch.setenv("FAKE_BUSCTL", mode)
        monkeypatch.setenv("FAKE_BUSCTL_PREEXISTING", preexisting)
        code, out, err = rig.run_cli(*CLEAN)
        assert code == osdless.EXIT_REFUSED, out
        assert "PowerDevil does not list its inhibition" in err
        assert rig.writes() == []
        assert not rig.last_clean().exists()
        assert not any(p.poll() is None for p in osdless.DETACHED), "the unconfirmed kde-inhibit is stopped"
    assert len(rig.helper_calls("kde-inhibit")) == 3


def test_kde_inhibit_is_given_time_to_register(rig, monkeypatch):
    monkeypatch.setenv("FAKE_KDE_REGISTER_DELAY", "0.5")
    code, out, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_OK, err
    assert "Holding kde-inhibit, systemd-inhibit for" in out
    assert len(rig.helper_calls("busctl")) > 2


def test_clean_with_one_working_inhibitor_goes_ahead(rig, monkeypatch):
    monkeypatch.setenv("FAKE_INHIBIT_FAIL", "kde-inhibit")
    code, out, _ = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_OK
    assert "Holding systemd-inhibit for" in out


def test_clean_bit_outside_the_mask_is_refused_before_any_side_effect(rig):
    rig.set_caps("FD(00FD)", "FD(00ED)")
    rig.set_reg("FD", 0x29, 0xED)
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_REFUSED
    assert rig.writes() == []
    assert rig.helper_calls("kde-inhibit") == []
    assert not rig.last_clean().exists()


def test_nothing_is_prepared_for_a_clean_the_last_gate_refuses(rig, monkeypatch):
    def offer(mon: osdless.Monitor, *args: object) -> None:
        mon.input_reason = ""

    monkeypatch.setattr(osdless.Monitor, "offer", offer)
    rig.set_caps("FD(00FD)", "FD(00ED)")
    rig.set_reg("FD", 0x29, 0xED)
    code, _, err = rig.run_cli(*CLEAN)
    assert code == osdless.EXIT_REFUSED
    assert "outside the capabilities mask" in err
    assert rig.writes() == []
    assert rig.helper_calls("systemd-inhibit") == []
    assert not rig.last_clean().exists()


def test_clean_is_never_retried(rig):
    rig.set_state(ignore_writes={"FD": -1})
    assert rig.run_cli(*CLEAN)[0] == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x39)]
    assert rig.last_clean().exists(), "an unconfirmed start still counts for the 20 h guard"


def test_clean_wait_polls_until_the_bit_clears(rig):
    rig.set_state(clean_clear_after=2)
    code, out, err = rig.run_cli(*CLEAN, "--wait")
    assert code == osdless.EXIT_OK, err
    assert "pixel cleaning finished" in out
    assert "still cleaning" in err
    assert rig.state()["regs"]["FD"][0] == 0x29
    assert rig.helper_calls("kde-inhibit") == [["--power", "--screenSaver", "sleep", "61"]]
    assert osdless.DETACHED and all(p.poll() is None for p in osdless.DETACHED), "the inhibitors run out on their own"


def test_clean_wait_keeps_the_inhibitors_when_the_bit_clears_at_once(rig, monkeypatch):
    # Nobody knows yet whether bit 0x10 stays set for a whole run, so a bit that clears at the first
    # poll must not let the PC sleep in the middle of one.
    monkeypatch.setenv("FAKE_INHIBIT_SLEEP", "30")
    rig.set_state(clean_clear_after=1)
    code, out, err = rig.run_cli(*CLEAN, "--wait")
    assert code == osdless.EXIT_OK, err
    assert "pixel cleaning finished after 0.0 min" in out
    assert "still cleaning" not in err
    assert len(osdless.DETACHED) == 2
    assert all(p.poll() is None for p in osdless.DETACHED), "inhibitors released"


def test_clean_json_prints_one_document(rig):
    code, out, err = rig.run_cli(*CLEAN, "--json")
    assert code == osdless.EXIT_OK, err
    assert json.loads(out) == {
        "command": "oled clean",
        "code": "0xFD",
        "old": 0x29,
        "new": 0x39,
        "written": True,
        "inhibitors": ["kde-inhibit", "systemd-inhibit"],
        "hold_seconds": 61,
    }


def test_clean_wait_json_prints_one_document(rig):
    rig.set_state(clean_clear_after=1)
    code, out, err = rig.run_cli(*CLEAN, "--wait", "--json")
    assert code == osdless.EXIT_OK, err
    document = json.loads(out)
    assert document["new"] == 0x39
    assert document["finished_after_min"] == 0.0
    rig.last_clean().unlink()
    rig.set_state(clean_clear_after=None, writes=[])
    rig.set_reg("FD", 0x29)
    code, out, err = rig.run_cli(*CLEAN, "--wait", "--json")
    assert code == osdless.EXIT_READ_FAILED
    document = json.loads(out)
    assert document["ok"] is False
    assert "0x0029 -> 0x0039, pixel cleaning started, but it was not seen finishing" in document["error"]


def test_clean_wait_tolerates_failed_polls(rig):
    rig.set_state(clean_clear_after=3, fail_read_numbers={"FD": [3]})
    assert rig.run_cli(*CLEAN, "--wait")[0] == osdless.EXIT_OK


def test_clean_wait_gives_up_after_the_cap(rig):
    code, _, err = rig.run_cli(*CLEAN, "--wait")
    assert code == osdless.EXIT_READ_FAILED
    assert "not seen finishing" in err
    assert rig.writes() == [(0xFD, 0x39)]


def test_clean_wait_keeps_the_inhibitors_unless_0xfd_reads_as_before(rig, monkeypatch):
    # Brightness's reply under 0xFD's opcode; the old value with a wrong max; a plausible value
    # other than the one before cleaning. None of them shows that cleaning ended.
    monkeypatch.setenv("FAKE_INHIBIT_SLEEP", "30")
    registers = copy.deepcopy(rig.state()["regs"])
    for reply in ([0x28, 0x64], [0x29, 0x64], [0x28, 0xFD]):
        rig.last_clean().unlink(missing_ok=True)
        rig.set_state(writes=[], clean_clear_after=1, clean_end_reg=reply, regs=copy.deepcopy(registers))
        code, out, err = rig.run_cli(*CLEAN, "--wait")
        assert code == osdless.EXIT_READ_FAILED, out
        assert "finished" not in out
        assert "not seen finishing" in err
        assert rig.writes() == [(0xFD, 0x39)]
        assert osdless.DETACHED and all(p.poll() is None for p in osdless.DETACHED), "inhibitors released"
        for proc in osdless.DETACHED:
            os.killpg(proc.pid, 9)
            proc.wait()
        osdless.DETACHED.clear()
