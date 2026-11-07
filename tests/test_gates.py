"""The safety gates, one by one, mostly below the command line so each gate is the only one in the way."""

import json

import pytest

import osdless
from rig import load_fixture

LIVE_CAPS = load_fixture("PG39WCDM")["capabilities"]
# Lists every never-write code with value 01, so the never-write gate is the only one left.
ALL_LISTED = LIVE_CAPS.replace("FF)", "FF " + " ".join(f"{code:02X}(01)" for code in sorted(osdless.NEVER_WRITE)) + ")")


def feature(name: str) -> osdless.Feature:
    return next(f for f in osdless.FEATURES if f.name == name)


def value_feature(code: int) -> osdless.Feature:
    return osdless.Feature(("test",), code, osdless.Write.VALUE, choices=(("one", 1),))


def monitor(caps=LIVE_CAPS, *, profile=osdless.ASUS_V2_GAMING, untested=None, writes=True):
    mon = osdless.Monitor(osdless.Ddcutil(writes=writes), untested=untested)
    mon.display = osdless.Display(9, "card1-DP-1", "AUS", "PG39WCDM", "SYNTHETIC0001")
    mon.link = osdless.Link("card1-DP-1", "DisplayPort")
    mon.caps = osdless.parse_caps(caps)
    mon.profile = profile
    mon.reasons = ("test",)
    mon.input_reason = ""
    return mon


def test_the_never_lists_are_exactly_these_codes():
    assert {0x01, 0x04, 0x05, 0x06, 0x08, 0x0A, 0xB0, 0xEC, 0xFE, 0xDD, 0xE8, 0xEB} == osdless.NEVER_SEND
    assert osdless.NEVER_SEND | {0x10, 0xFC, 0xE2, 0xC5, 0xD6, 0xED, 0x47, 0xF2} == osdless.NEVER_WRITE


def test_no_profile_writes_a_never_write_code():
    for profile in (osdless.ASUS_V2_GAMING, osdless.READ_ONLY):
        assert {f.code for f in profile.features} & osdless.NEVER_WRITE == set()
    assert osdless.READ_ONLY.features == ()
    assert not osdless.READ_ONLY.writes


def test_every_table_entry_is_hashable():
    entries = (*osdless.FEATURES, *osdless.ASUS_STATUS, *osdless.MCCS_STATUS, osdless.ASUS_V2_GAMING, osdless.READ_ONLY)
    for entry in entries:
        hash(entry)
    assert len(set(osdless.FEATURES)) == len(osdless.FEATURES)


def test_the_write_set_is_the_reviewed_one():
    assert [f.name for f in osdless.FEATURES] == [
        "oled saver",
        "oled logo",
        "oled uniform",
        "oled clear-edge",
        "oled clean",
        "oled reminder",
        "oled screen-move",
        "power",
        "kvm",
        "input",
    ]
    assert {("PG39WCDM", "oled uniform")} == osdless.VERIFIED
    assert {0xFD} == osdless.BITFIELDS
    named = dict(osdless.FD_BITS)
    for f in osdless.FEATURES:
        if f.write in osdless.BIT_WRITES:
            assert f.bit in named and f.bit <= 0xFF, "only the low-byte OLED bits are written"


def test_never_write_codes_are_refused_even_if_made_writable(rig):
    features = tuple(value_feature(code) for code in sorted(osdless.NEVER_WRITE))
    profile = osdless.Profile("test", True, (), osdless.FEATURES + features, frozenset({0xFD}))
    mon = monitor(ALL_LISTED, profile=profile, untested="PG39WCDM")
    for f in features:
        mon._fresh[f.code] = 0
        with pytest.raises(osdless.Refused) as caught:
            mon.set_value(f, 1, "test")
        assert caught.value.exit_code == osdless.EXIT_REFUSED
        assert "never written" in str(caught.value)
    assert rig.calls() == []


def test_codes_outside_the_writable_set_are_refused(rig):
    mon = monitor(untested="PG39WCDM")
    for code in (0x12, 0x62, 0x14, 0xCC, 0xDC, 0x87, 0xF8):
        mon._fresh[code] = 0
        with pytest.raises(osdless.Refused, match="not something osdless writes"):
            mon.set_value(value_feature(code), 1, "test")
    assert rig.calls() == []


def test_read_only_profile_is_never_written(rig):
    mon = monitor(profile=osdless.READ_ONLY, untested="PG39WCDM")
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="read-only"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []


def test_never_send_reads_are_refused_even_if_listed(rig):
    mon = monitor(ALL_LISTED)
    for code in sorted(osdless.NEVER_SEND):
        with pytest.raises(osdless.Refused):
            mon.read([0x12, code])
    assert rig.calls() == []


def test_only_three_reads_and_setvcp_reach_ddcutil(rig):
    for sub in ("scs", "loadvcp", "dumpvcp", "discard", "environment", "chkusbmon", "--version"):
        with pytest.raises(osdless.Refused):
            osdless.Ddcutil(writes=True).run(sub, [], 1)
    assert rig.calls() == []


def test_a_read_only_runner_cannot_send_setvcp(rig):
    with pytest.raises(osdless.Refused, match="only reads"):
        osdless.Ddcutil().run("setvcp", ["--bus", "9", "--noverify", "0xFD", "0x69"], 1)
    mon = monitor(writes=False)
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="only reads"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []


def test_read_commands_get_a_read_only_runner():
    parser = osdless.build_parser()
    for argv in (["list"], ["status"], ["get", "FD"], ["features"], ["report"]):
        assert parser.parse_args(argv).writes is False
    for argv in (["oled", "uniform", "on"], ["oled", "clean"], ["power", "saving"], ["kvm", "usb-b"]):
        assert parser.parse_args(argv).writes is True


def test_write_needs_a_fresh_read_and_consumes_it(rig):
    mon = monitor(untested="PG39WCDM")
    reminder = feature("oled reminder")
    with pytest.raises(osdless.Refused, match="no fresh read"):
        mon.set_value(reminder, 2, "test")
    mon._fresh[0xF8] = 8
    mon.set_value(reminder, 2, "test")
    with pytest.raises(osdless.Refused, match="no fresh read"):
        mon.set_value(reminder, 4, "test")
    assert rig.writes() == [(0xF8, 2)]


def test_only_clean_may_set_the_cleaning_bit(rig):
    mon = monitor(untested="PG39WCDM")
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="only `oled clean`"):
        mon.set_value(feature("oled uniform"), 0x39, "oled uniform on")
    assert rig.calls() == []
    mon._fresh[0xFD] = 0x29
    mon.set_value(osdless.CLEAN, 0x39, "oled clean")
    assert rig.writes() == [(0xFD, 0x39)]


def test_a_bit_write_changes_only_its_own_bit(rig):
    mon = monitor(untested="PG39WCDM")
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="not oled uniform's bit 0x0040"):
        mon.set_value(feature("oled uniform"), 0x21, "test")
    assert rig.calls() == []


def test_no_fd_write_starts_from_a_running_clean(rig):
    mon = monitor(untested="PG39WCDM")
    for f, value in ((feature("oled uniform"), 0x79), (feature("oled saver"), 0x31), (osdless.CLEAN, 0x29)):
        mon._fresh[0xFD] = 0x39
        with pytest.raises(osdless.Refused, match="already running"):
            mon.set_value(f, value, "test")
    assert rig.calls() == []


def test_fd_bits_outside_the_mask_are_never_written_back(rig):
    mon = monitor()
    mon._fresh[0xFD] = 0x2B
    with pytest.raises(osdless.Refused, match="outside the capabilities mask"):
        mon.set_value(feature("oled uniform"), 0x6B, "test")
    assert rig.calls() == []


def test_bit_writes_change_exactly_one_bit(rig):
    mon = monitor(untested="PG39WCDM")
    for value in (0x29, 0x29 | 0x40 | 0x80, 0x00):
        mon._fresh[0xFD] = 0x29
        with pytest.raises(osdless.Refused, match="exactly one bit"):
            mon.set_value(feature("oled uniform"), value, "test")
    assert rig.writes() == []


def test_values_above_a_byte_only_carry_the_fd_high_byte_through(rig):
    mon = monitor(LIVE_CAPS.replace("FD(00FD)", "FD(7879)").replace("F8(00 02 04 08)", "F8(00 02 04 08 108)"))
    mon.untested = "PG39WCDM"
    mon._fresh[0xF8] = 8
    with pytest.raises(osdless.Refused, match="only sent to carry"):
        mon.set_value(feature("oled reminder"), 0x108, "test")
    mon._fresh[0xFD] = 0x0829
    with pytest.raises(osdless.Refused, match="only sent to carry"):
        mon.set_value(feature("oled uniform"), 0x0869 ^ 0x1000, "test")
    mon._fresh[0xFD] = 0x0829
    with pytest.raises(osdless.Refused, match="does not fit"):
        mon.set_value(feature("oled uniform"), 0x10869, "test")
    assert rig.calls() == []
    mon._fresh[0xFD] = 0x0829
    mon.set_value(feature("oled uniform"), 0x0869, "test")
    assert rig.state()["write_args"] == ["0x0869"]


def test_a_value_must_be_one_of_the_features_own(rig):
    mon = monitor(LIVE_CAPS.replace("F8(00 02 04 08)", "F8(00 02 03 04 08)"), untested="PG39WCDM")
    mon._fresh[0xF8] = 8
    with pytest.raises(osdless.Refused, match="not one of oled reminder's values"):
        mon.set_value(feature("oled reminder"), 3, "test")
    assert rig.calls() == []


def test_a_value_must_be_in_the_capabilities(rig):
    mon = monitor(LIVE_CAPS.replace("F8(00 02 04 08)", "F8(00 02 04)"), untested="PG39WCDM")
    mon._fresh[0xF8] = 2
    with pytest.raises(osdless.Refused, match=r"not among 0xF8's capability values \(00 02 04\)"):
        mon.set_value(feature("oled reminder"), 8, "test")
    assert rig.calls() == []


def test_code_missing_from_the_capabilities_is_never_written(rig):
    mon = monitor(LIVE_CAPS.replace(" F9(00 01 02 03)", ""), untested="PG39WCDM")
    mon._fresh[0xF9] = 3
    with pytest.raises(osdless.Refused, match="not in the monitor's capabilities"):
        mon.set_value(feature("oled screen-move"), 1, "test")
    assert rig.calls() == []


def test_unidentified_monitor_is_never_written(rig):
    mon = monitor(untested="PG39WCDM")
    mon.display = None
    mon._fresh[0xF8] = 8
    with pytest.raises(osdless.Refused, match="identity"):
        mon.set_value(feature("oled reminder"), 2, "test")
    assert rig.calls() == []


def test_unmapped_bus_is_never_written(rig):
    mon = monitor()
    mon.link = None
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="not the DDC/CI adapter of a connected DRM connector"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []


def test_a_monitor_chosen_without_its_serial_is_never_written(rig):
    mon = monitor()
    mon.write_block = "no serial"
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="no serial"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []


def test_a_dp_write_needs_the_monitors_own_input_read_first(rig):
    mon = monitor()
    mon.input_reason = None
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match=r"input \(0x60\) has not been read"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    mon.input_reason = "the monitor's own input is HDMI"
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="the monitor's own input is HDMI"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []


def test_the_oled_writes_need_an_oled_at_the_last_gate_too(rig):
    mon = monitor(LIVE_CAPS.replace("model(PG39WCDM)", "model(PG27AQN)"), untested="PG27AQN")
    mon.display = osdless.Display(9, "card1-DP-1", "AUS", "PG27AQN", "SYNTHETIC0099")
    for name, code, value in (("oled uniform", 0xFD, 0x69), ("oled clean", 0xFD, 0x39), ("oled reminder", 0xF8, 2)):
        mon._fresh[code] = 0x29 if code == 0xFD else 8
        with pytest.raises(osdless.Refused, match="PG27AQN is not an OLED"):
            mon.set_value(feature(name), value, "test")
    assert rig.calls() == []
    mon._fresh[0xE1] = 1
    mon.set_value(feature("power"), 0, "test")
    assert rig.writes() == [(0xE1, 0)]


def test_asus_names_an_oled_by_its_7th_character():
    for model in ("PG39WCDM", "PG34WCDM", "PG32UCDM", "PG49WCD", "XG27ACDNG", "PG27AQDM", "pg32ucwm", "VG27AQDW"):
        assert osdless.asus_oled(model), model
    for model in ("PG27AQN", "VG27AQ3A", "XG27AQ", "PA32UCDM", "PG27AD", "", "VA27DQDM"):
        assert not osdless.asus_oled(model), model


def test_a_monitor_from_another_maker_is_never_sent_0xef(rig):
    other = load_fixture("PG39WCDM")
    other["display"]["mfg"] = "XYZ"
    other["display"]["model"] = "GEN27"
    other["capabilities"] = other["capabilities"].replace("model(PG39WCDM)", "model(GEN27)")
    rig.use(other)
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.run_cli("features")[0] == osdless.EXIT_OK
    sent = [arg for call in rig.calls() if call["command"] == "getvcp" for arg in call["argv"]]
    assert "0xEF" not in sent
    assert "0x10" in sent


def test_unverified_writes_are_refused_at_the_last_gate_too(rig):
    mon = monitor()
    for name, value in (("oled saver", 0x21), ("oled logo", 0x09)):
        mon._fresh[0xFD] = 0x29
        with pytest.raises(osdless.Refused, match=f"{name} has not been verified on the PG39WCDM"):
            mon.set_value(feature(name), value, "test")
    mon.link = osdless.Link("card1-HDMI-A-1", "HDMI")
    mon._fresh[0xFD] = 0x29
    with pytest.raises(osdless.Refused, match="writes over HDMI"):
        mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.calls() == []
    mon.untested = "PG39WCDM"
    mon._fresh[0xFD] = 0x29
    mon.set_value(feature("oled uniform"), 0x69, "test")
    assert rig.writes() == [(0xFD, 0x69)]


def test_every_ddcutil_call_keeps_the_users_configuration_out(rig, tmp_path):
    rig.run_cli("list")
    rig.run_cli("status")
    rig.run_cli("oled", "uniform", "on")
    rig.run_cli("report", "--output", str(tmp_path))
    commands = set()
    for call in rig.calls():
        assert call["argv"][:3] == list(osdless.DDCUTIL_OPTIONS)
        for flag in ("--disable-flock", "--disable-cross-instance-locks", "--enable-udf", "--force"):
            assert flag not in call["argv"]
        commands.add(call["command"])
    assert commands == {"detect", "capabilities", "getvcp", "setvcp", "--version"}


def test_the_fake_ddcutil_refuses_calls_without_those_options(rig, monkeypatch):
    monkeypatch.setattr(osdless, "DDCUTIL_OPTIONS", ("--noconfig", "--enable-cross-instance-locks"))
    assert rig.run_cli("status")[0] == osdless.EXIT_READ_FAILED
    assert rig.state()["violations"] == ["detect without --disable-udf"]
    rig.set_state(violations=[])


def test_only_ff_and_ef_are_read_before_the_capabilities(rig):
    mon = monitor()
    mon.caps = None
    for code in (0x10, 0xFD, 0xDC):
        with pytest.raises(osdless.Refused, match="before the capabilities are known"):
            mon.read([0xFF, code], bootstrap=True)
        with pytest.raises(osdless.Refused, match="not in the monitor's capabilities"):
            mon.read([code])
    assert rig.calls() == []


def test_a_bitfield_needs_its_mask_to_be_plausible():
    mon = monitor(LIVE_CAPS.replace("FD(00FD)", "FD"))
    assert not mon.plausible(0xFD, osdless.Reading(0xFD, 0, 0, 0, 0))
    mon = monitor()
    assert mon.plausible(0xFD, osdless.Reading(0xFD, 0, 0xFD, 0, 0))
    assert not mon.plausible(0xFD, osdless.Reading(0xFD, 0, 0xFF, 0, 0x29))


def test_ddcutil_version_is_one_exact_call(rig):
    assert osdless.Ddcutil().version() == "3.0.2"
    assert [c["argv"] for c in rig.calls()] == [[*osdless.DDCUTIL_OPTIONS, "--version"]]


def test_status_json_never_hides_a_read_only_reason(rig):
    rig.use("asus-old-table")
    doc = json.loads(rig.run_cli("status", "--json")[1])
    assert doc["profile"]["writes"] is False
    assert doc["profile"]["reasons"][-1].startswith("VCP table 1.3 is not table v2")
    assert doc["vcp_table"] == "1.3"
