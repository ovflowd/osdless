import contextlib
import io
import json

import pytest

import osdless
from rig import REAL, fixture_names, load_fixture

UNTESTED = ("--untested", "PG39WCDM")
WRITABLE = [name for name in fixture_names() if load_fixture(name)["expect"]["profile"] == "asus-v2-gaming"]
READ_ONLY = [name for name in fixture_names() if name not in WRITABLE]


def test_oled_uniform_is_the_only_write_without_untested(rig):
    for argv in (
        ("oled", "saver", "off"),
        ("oled", "logo", "off"),
        ("oled", "clear-edge", "on"),
        ("oled", "reminder", "2"),
        ("oled", "screen-move", "light"),
        ("power", "standard"),
        ("kvm", "usb-c", "--yes"),
        ("input", "hdmi1", "--yes"),
        ("oled", "clean", "--yes"),
    ):
        code, _, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_REFUSED, argv
        name = " ".join(argv[:2]) if argv[0] == "oled" else argv[0]
        assert f"{name} has not been verified on the PG39WCDM" in err
        assert "re-run with --untested PG39WCDM" in err
    reads = rig.state()["reads"]
    assert set(reads) <= {"FF", "EF"}, "nothing is read for a write that is refused anyway"
    assert rig.writes() == []
    assert not rig.last_clean().exists()
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_OK, err
    assert "untested" not in err
    assert rig.writes() == [(0xFD, 0x69)]


def test_untested_writes_say_so(rig):
    code, _, err = rig.run_cli("oled", "saver", "off", *UNTESTED)
    assert code == osdless.EXIT_OK
    assert "untested: oled saver has not been verified on the PG39WCDM; going ahead because of --untested" in err
    assert rig.writes() == [(0xFD, 0x21)]


def test_each_toggle_is_a_read_modify_write_of_one_bit(rig):
    for name, state, value in (
        ("saver", "off", 0x21),
        ("logo", "off", 0x09),
        ("uniform", "on", 0x69),
        ("clear-edge", "on", 0xA9),
    ):
        rig.set_state(writes=[])
        rig.set_reg("FD", 0x29)
        code, out, err = rig.run_cli("oled", name, state, *UNTESTED)
        assert code == osdless.EXIT_OK, err
        assert rig.writes() == [(0xFD, value)]
        assert rig.state()["regs"]["FD"][0] == value
        assert f"0x0029 -> 0x{value:04X}, confirmed" in out


def test_bit_already_in_the_requested_state_writes_nothing(rig):
    code, out, _ = rig.run_cli("oled", "saver", "on", *UNTESTED)
    assert code == osdless.EXIT_OK
    assert "nothing written" in out
    assert rig.writes() == []


def test_failed_read_aborts_without_writing(rig):
    rig.set_state(fail_reads={"FD": 1})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_READ_FAILED
    assert "reading 0xFD failed" in err
    assert rig.writes() == []


def test_not_available_marker_aborts_without_writing(rig):
    rig.set_reg("FD", 0xFE, 0xFE)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_READ_FAILED
    assert "0xFE/0xFE" in err
    assert rig.writes() == []


def test_implausible_read_is_read_twice_then_abandoned(rig):
    rig.set_reg("FD", 0x0129)
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_READ_FAILED
    assert rig.writes() == []
    assert rig.state()["reads"]["FD"] == 2


def test_implausible_replies_are_not_written_from(rig):
    # A max that is not the mask (all zeros, or contrast's reply under 0xFD's opcode as the 0xDD alias
    # test saw), and a bit outside the mask. Toggling a bit of any of them would rewrite VRR, saver and logo.
    for cur, mx in ((0x0000, 0x0000), (0x0050, 0x0064), (0x00FE, 0x00FD)):
        rig.set_state(reads={})
        rig.set_reg("FD", cur, mx)
        code, _, err = rig.run_cli("oled", "uniform", "on")
        assert code == osdless.EXIT_READ_FAILED, err
        assert "not a plausible value" in err
        assert rig.state()["reads"]["FD"] == 2
    assert rig.writes() == []


def test_every_bit_off_is_a_plausible_state(rig):
    rig.set_reg("FD", 0x0000)
    code, out, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_OK, err
    assert "0x0000 -> 0x0040, confirmed" in out
    assert rig.writes() == [(0xFD, 0x40)]


def test_toggles_are_refused_while_cleaning_runs(rig):
    rig.set_reg("FD", 0x39)
    for argv in (("saver", "off"), ("logo", "off"), ("uniform", "on"), ("clear-edge", "on"), ("saver", "on")):
        code, _, err = rig.run_cli("oled", *argv, *UNTESTED)
        assert code == osdless.EXIT_REFUSED
        assert "pixel cleaning is already running" in err
    assert rig.writes() == []
    assert rig.helper_calls("logger") == []


def test_read_back_with_other_bits_changed_is_not_written_again(rig):
    # The OSD turns VRR off as the first write is lost: a retry rebuilt from the read-back would
    # write a state nobody asked for.
    rig.set_state(ignore_writes={"FD": 1}, set_after_write={"FD": [0x28, 0xFD]})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69)]
    assert "read back 0x0028" in err
    assert "not written again" in err


def test_phantom_read_back_is_not_written_from(rig):
    rig.set_state(ignore_writes={"FD": 1}, set_after_write={"FD": [0x1E, 0x64]})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69)]
    assert "nothing safe to retry from" in err


def test_read_back_matching_only_by_value_is_not_a_confirmation(rig):
    rig.set_state(ignore_writes={"FD": 1}, set_after_write={"FD": [0x69, 0x64]})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69)]
    assert "nothing safe to retry from" in err


def test_bit_outside_the_capabilities_mask_is_refused(rig):
    rig.set_caps("FD(00FD)", "FD(00BD)")
    rig.set_reg("FD", 0x29, 0xBD)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "bit 0x0040 is not in the capabilities mask 0x00BD" in err
    assert rig.writes() == []


def test_write_that_does_not_stick_is_retried_once(rig):
    rig.set_state(ignore_writes={"FD": 1})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_OK, err
    assert rig.writes() == [(0xFD, 0x69), (0xFD, 0x69)]


def test_write_that_never_sticks_exits_4_after_one_retry(rig):
    rig.set_state(ignore_writes={"FD": -1})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69), (0xFD, 0x69)]
    assert "old=0x0029 new=0x0069 read back 0x0029" in err


def test_failed_read_back_is_read_again_not_written_again(rig):
    rig.set_state(fail_read_numbers={"FD": [2]})
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xFD, 0x69)]


def test_read_back_failing_twice_exits_4_without_a_blind_retry(rig):
    rig.set_state(fail_read_numbers={"FD": [2, 3]})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69)]
    assert "nothing safe to retry from" in err


def test_other_bits_changing_under_the_write_is_not_confirmed(rig):
    rig.set_state(write_side_effect_xor={"FD": 0x01})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xFD, 0x69)]
    assert "read back 0x0068" in err
    assert "not written again" in err


def test_read_back_waits_at_least_100_ms(rig):
    rig.run_cli("oled", "uniform", "on")
    calls = rig.calls()
    index = next(i for i, c in enumerate(calls) if c["command"] == "setvcp")
    assert calls[index + 1]["command"] == "getvcp"
    assert calls[index + 1]["start"] - calls[index]["end"] >= 0.1


def test_old_vcp_table_refuses_writes(rig):
    rig.set_reg("EF", 0xB1, 0x0107)
    code, _, err = rig.run_cli("oled", "uniform", "on", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "VCP table 1.7 is not table v2" in err
    assert rig.writes() == []


def test_setvcp_sends_one_byte_without_ddcutil_verify(rig):
    rig.run_cli("oled", "uniform", "on")
    call = next(c for c in rig.calls() if c["command"] == "setvcp")
    assert call["argv"] == [*osdless.DDCUTIL_OPTIONS, "setvcp", "--bus", "9", "--noverify", "0xFD", "0x69"]


def test_write_result_as_json(rig):
    code, out, _ = rig.run_cli("oled", "uniform", "on", "--json")
    assert code == osdless.EXIT_OK
    assert json.loads(out) == {"command": "oled uniform on", "code": "0xFD", "old": 0x29, "new": 0x69, "written": True}


def test_reminder_values(rig):
    for word, value in (("off", 0), ("2", 2), ("4", 4), ("8", 8)):
        rig.set_state(writes=[])
        rig.set_reg("F8", 0 if value else 8)
        code, _, err = rig.run_cli("oled", "reminder", word, *UNTESTED)
        assert code == osdless.EXIT_OK, err
        assert rig.writes() == [(0xF8, value)]


def test_screen_move_values(rig):
    for word, value in (("off", 0), ("light", 1), ("middle", 2), ("strong", 3)):
        rig.set_state(writes=[])
        rig.set_reg("F9", 3 if value != 3 else 0)
        assert rig.run_cli("oled", "screen-move", word, *UNTESTED)[0] == osdless.EXIT_OK
        assert rig.writes() == [(0xF9, value)]


def test_power_setting(rig):
    assert rig.run_cli("power", "saving", *UNTESTED)[0] == osdless.EXIT_OK
    assert rig.writes() == []
    assert rig.run_cli("power", "standard", *UNTESTED)[0] == osdless.EXIT_OK
    assert rig.run_cli("power", "saving", *UNTESTED)[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xE1, 0), (0xE1, 1)]


def test_value_missing_from_the_capabilities_is_refused(rig):
    rig.set_caps("F8(00 02 04 08)", "F8(00 02 04)")
    rig.set_reg("F8", 2)
    code, _, err = rig.run_cli("oled", "reminder", "8", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "oled reminder 8 (0xF8 = 08) is not in this monitor's capabilities: F8(00 02 04)" in err
    assert rig.writes() == []


def test_code_missing_from_the_capabilities_is_refused(rig):
    rig.set_caps(" F9(00 01 02 03)", "")
    code, _, err = rig.run_cli("oled", "screen-move", "light", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "0xF9 is not in this monitor's capabilities" in err
    assert rig.writes() == []


def test_implausible_current_value_is_read_twice_then_abandoned(rig):
    rig.set_reg("F8", 5)
    assert rig.run_cli("oled", "reminder", "2", *UNTESTED)[0] == osdless.EXIT_READ_FAILED
    assert rig.state()["reads"]["F8"] == 2
    assert rig.writes() == []


def test_value_write_that_never_sticks_exits_4(rig):
    rig.set_state(ignore_writes={"F9": -1})
    assert rig.run_cli("oled", "screen-move", "light", *UNTESTED)[0] == osdless.EXIT_WRITE_UNCONFIRMED
    assert rig.writes() == [(0xF9, 1), (0xF9, 1)]


def test_kvm_needs_yes(rig):
    code, _, err = rig.run_cli("kvm", "usb-c", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "every USB device on it" in err
    assert rig.calls() == []


def test_kvm_with_yes(rig):
    for target, value in (("usb-c", 3), ("smart", 1), ("usb-b", 2)):
        assert rig.run_cli("kvm", target, "--yes", *UNTESTED)[0] == osdless.EXIT_OK
        assert rig.writes()[-1] == (0xF3, value)


def test_kvm_target_missing_from_the_capabilities_is_refused(rig):
    rig.set_caps("F3(01 02 03)", "F3(02 03)")
    code, _, err = rig.run_cli("kvm", "smart", "--yes", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "kvm smart (0xF3 = 01) is not in this monitor's capabilities: F3(02 03)" in err
    assert rig.writes() == []


def test_input_needs_yes(rig):
    code, _, err = rig.run_cli("input", "hdmi1", *UNTESTED)
    assert code == osdless.EXIT_REFUSED
    assert "blanks this screen" in err
    assert rig.calls() == []


def test_input_with_yes(rig):
    for source, value in (("hdmi1", 0x11), ("hdmi2", 0x12), ("usbc", 0x1A), ("dp", 0x0F)):
        assert rig.run_cli("input", source, "--yes", *UNTESTED)[0] == osdless.EXIT_OK
        assert rig.writes()[-1] == (0x60, value)


def test_switches_are_sent_once_even_if_the_read_back_disagrees(rig):
    # Auto Input Detection can bounce an input with no signal back to DP; a hub move can lag.
    for argv, code, ignore, value in (
        (("kvm", "usb-c", "--yes"), 0xF3, 1, 3),
        (("input", "hdmi1", "--yes"), 0x60, -1, 0x11),
    ):
        rig.set_state(writes=[], ignore_writes={f"{code:02X}": ignore})
        status, _, err = rig.run_cli(*argv, *UNTESTED)
        assert status == osdless.EXIT_WRITE_UNCONFIRMED
        assert rig.writes() == [(code, value)]
        assert "may still be in progress, so it is not sent again" in err


def test_switch_read_back_waits_longer_than_a_setting(rig, monkeypatch):
    assert REAL["SWITCH_SETTLE"] >= 1.0
    monkeypatch.setattr(osdless, "SWITCH_SETTLE", 0.8)
    for argv in (("kvm", "usb-c", "--yes"), ("input", "hdmi1", "--yes")):
        rig.forget_calls()
        assert rig.run_cli(*argv, *UNTESTED)[0] == osdless.EXIT_OK
        calls = rig.calls()
        index = next(i for i, c in enumerate(calls) if c["command"] == "setvcp")
        assert calls[index + 1]["start"] - calls[index]["end"] >= 0.8


def test_unknown_choices_are_rejected_by_the_parser(rig):
    with contextlib.redirect_stderr(io.StringIO()):
        for argv in (
            ["oled", "reminder", "3"],
            ["brightness", "50"],
            ["kvm", "auto", "--yes"],
            ["oled", "taskbar", "on"],
        ):
            assert osdless.main(argv) == osdless.EXIT_REFUSED
    assert rig.calls() == []


def test_a_16_bit_write_needs_untested_even_for_a_verified_feature(rig):
    rig.set_caps("FD(00FD)", "FD(08FD)")
    rig.set_reg("FD", 0x0829, 0x08FD)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "16-bit writes have not been verified on any monitor" in err
    assert rig.writes() == []
    code, _, err = rig.run_cli("oled", "uniform", "on", *UNTESTED)
    assert code == osdless.EXIT_OK, err
    assert rig.writes() == [(0xFD, 0x0869)]
    assert rig.state()["write_args"] == ["0x0869"]


def test_xg27acdng_keeps_the_high_byte_through_a_16_bit_write(rig):
    rig.use("XG27ACDNG")
    code, out, err = rig.run_cli("oled", "uniform", "on", "--untested", "XG27ACDNG")
    assert code == osdless.EXIT_OK, err
    assert "0x0829 -> 0x0869, confirmed" in out
    assert rig.state()["write_args"] == ["0x0869"]
    assert "untested: 16-bit writes" in err


def test_pg34wcdm_offers_only_the_kvm_targets_it_lists(rig):
    rig.use("PG34WCDM")
    code, _, err = rig.run_cli("kvm", "smart", "--yes", "--untested", "PG34WCDM")
    assert code == osdless.EXIT_REFUSED
    assert "F3(02 03)" in err
    assert rig.run_cli("kvm", "usb-c", "--yes", "--untested", "PG34WCDM")[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xF3, 3)]


def test_pg49wcd_has_no_power_setting(rig):
    rig.use("PG49WCD")
    code, _, err = rig.run_cli("power", "saving", "--untested", "PG49WCD")
    assert code == osdless.EXIT_REFUSED
    assert "0xE1 is not in this monitor's capabilities" in err


def test_xg27acdng_lists_screen_move_without_values(rig):
    rig.use("XG27ACDNG")
    code, _, err = rig.run_cli("oled", "screen-move", "light", "--untested", "XG27ACDNG")
    assert code == osdless.EXIT_REFUSED
    assert "the capabilities list none of its values: F9()" in err
    assert rig.writes() == []


@pytest.mark.parametrize("name", WRITABLE)
def test_each_writable_fixture_takes_one_write(rig, name):
    rig.use(name)
    old = rig.fixture["registers"]["FD"][0]
    untested = () if name == "PG39WCDM" else ("--untested", rig.model)
    code, out, err = rig.run_cli("oled", "uniform", "on", *untested)
    assert code == osdless.EXIT_OK, err
    assert rig.writes() == [(0xFD, old | 0x40)]
    assert "confirmed" in out


@pytest.mark.parametrize("name", READ_ONLY)
def test_each_read_only_fixture_refuses_every_write(rig, name):
    rig.use(name)
    code, _, err = rig.run_cli("oled", "uniform", "on", "--untested", rig.model)
    assert code == osdless.EXIT_REFUSED
    assert "read-only" in err
    assert rig.writes() == []
    assert set(rig.state()["reads"]) <= {"EF"}
