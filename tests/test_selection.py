import shutil

import osdless

SERIAL = "SYNTHETIC0001"
TWO_MONITORS = [
    {"bus": 9, "connector": "card1-DP-1", "monitor": f"AUS:PG39WCDM:{SERIAL}"},
    {"bus": 10, "connector": "card1-DP-2", "monitor": "AUS:PG34WCDM:SYNTHETIC0002"},
]


def test_a_serial_that_matches_nothing_is_a_read_failure(rig):
    code, _, err = rig.run_cli("oled", "uniform", "on", "--serial", "SYNTHETIC9999")
    assert code == osdless.EXIT_READ_FAILED
    assert "no detected display matches --serial SYNTHETIC9999" in err
    assert rig.subcommands() == ["detect"]


def test_a_model_that_matches_nothing_is_a_read_failure(rig):
    assert rig.run_cli("status", "--model", "PG34WCDM")[0] == osdless.EXIT_READ_FAILED
    assert rig.subcommands() == ["detect"]


def test_selectors_pick_one_of_several_monitors(rig):
    rig.set_state(displays=TWO_MONITORS)
    code, _, err = rig.run_cli("status")
    assert code == osdless.EXIT_REFUSED
    assert "2 displays match" in err
    assert "PG39WCDM on /dev/i2c-9 (card1-DP-1); PG34WCDM on /dev/i2c-10 (card1-DP-2)" in err
    for selector in (
        ("--serial", SERIAL),
        ("--model", "pg39wcdm"),
        ("--connector", "card1-DP-1"),
        ("--connector", "DP-1"),
        ("--bus", "9"),
        ("--model", "PG39WCDM", "--bus", "9"),
    ):
        code, out, err = rig.run_cli("status", *selector)
        assert code == osdless.EXIT_OK, (selector, err)
        assert f"AUS:PG39WCDM:{SERIAL} on /dev/i2c-9" in out
    assert rig.run_cli("status", "--model", "PG39WCDM", "--bus", "10")[0] == osdless.EXIT_READ_FAILED
    assert rig.run_cli("status", "--connector", "HDMI-A-1")[0] == osdless.EXIT_READ_FAILED


def test_the_n_displays_refusal_leaves_out_the_serial_numbers(rig, tmp_path):
    rig.set_state(displays=TWO_MONITORS)
    for argv in (("report", "--output", str(tmp_path)), ("get", "FD"), ("oled", "uniform", "on", "--json")):
        code, out, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_REFUSED
        assert "2 displays match" in err
        assert "SYNTHETIC" not in out + err
    assert rig.subcommands() == ["detect"] * 3


def test_a_monitor_on_the_separate_ddc_adapter_of_a_dp_connector_is_only_read(rig):
    # amdgpu: card1-DP-1/ddc points at i2c-4, not at its DP-AUX adapter i2c-9. A passive
    # DisplayPort-to-HDMI adapter puts the monitor there.
    rig.set_state(bus=4, displays=[{"bus": 4, "connector": "card1-DP-1", "monitor": f"AUS:PG39WCDM:{SERIAL}"}])
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    assert f"AUS:PG39WCDM:{SERIAL} on /dev/i2c-4 (the `ddc` adapter of card1-DP-1)" in out
    code, out, _ = rig.run_cli("list")
    assert out == f"/dev/i2c-4   AUS:PG39WCDM:{SERIAL} (the `ddc` adapter of card1-DP-1: read-only)\n"
    code, out, _ = rig.run_cli("features")
    assert "the `ddc` adapter of card1-DP-1, not its DP-AUX adapter" in out
    assert "ready" not in out and "--untested" not in out
    reads = rig.state()["reads"]
    for argv in (("oled", "uniform", "on"), ("oled", "saver", "off", "--untested", "PG39WCDM")):
        code, _, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_REFUSED
        assert "so osdless only reads this monitor" in err
    after = rig.state()["reads"]
    assert (after["FD"], after["60"]) == (reads["FD"], reads["60"]), "nothing is read for a refused write"
    assert rig.writes() == []


def test_a_dp_connector_whose_ddc_symlink_is_its_own_aux_adapter_is_written(rig):
    # i915 and xe create DisplayPort connectors with their DP-AUX adapter as `ddc`.
    ddc = rig.drm / "card1-DP-1" / "ddc"
    ddc.unlink()
    ddc.symlink_to("i2c-9")
    drm = osdless.map_connectors()
    assert drm.links == {9: osdless.Link("card1-DP-1", "DisplayPort")}
    assert drm.dp_ddc == {}
    for argv in (("status",), ("features",), ("get", "10")):
        code, _, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_OK, (argv, err)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_OK, err
    assert rig.writes() == [(0xFD, 0x69)]


def test_connector_mismatch_is_refused(rig):
    rig.set_state(displays=[{"bus": 9, "connector": "card1-DP-2", "monitor": f"AUS:PG39WCDM:{SERIAL}"}])
    assert rig.run_cli("status")[0] == osdless.EXIT_REFUSED
    assert rig.subcommands() == ["detect"]


def test_two_identical_monitors_need_a_location(rig):
    twice = [
        {"bus": 9, "connector": "card1-DP-1", "monitor": f"AUS:PG39WCDM:{SERIAL}"},
        {"bus": 10, "connector": "card1-DP-2", "monitor": f"AUS:PG39WCDM:{SERIAL}"},
    ]
    rig.set_state(displays=twice)
    for argv in (("oled", "uniform", "on"), ("oled", "uniform", "on", "--serial", SERIAL), ("status",)):
        code, _, err = rig.run_cli(*argv)
        assert code == osdless.EXIT_REFUSED
        assert "2 displays match" in err
    assert rig.subcommands() == ["detect"] * 3
    assert rig.run_cli("status", "--bus", "9")[0] == osdless.EXIT_OK
    assert rig.run_cli("oled", "uniform", "on", "--connector", "DP-1")[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xFD, 0x69)]


def test_a_monitor_without_a_serial_is_written_only_when_chosen_by_location(rig):
    rig.set_state(displays=[{"bus": 9, "connector": "card1-DP-1", "monitor": "AUS:PG39WCDM:"}])
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "no serial number" in err
    assert rig.writes() == []
    assert rig.run_cli("oled", "uniform", "on", "--bus", "9")[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xFD, 0x69)]


def test_detect_failure_is_a_read_failure(rig):
    rig.set_state(detect_fail=True)
    assert rig.run_cli("status")[0] == osdless.EXIT_READ_FAILED


def test_a_bus_without_a_drm_connector_is_read_only(rig):
    (rig.drm / "card1-DP-1" / "status").write_text("disconnected\n")
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    assert "(not mapped to a DRM connector)" in out
    reads = rig.state()["reads"]["FD"]
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "not the DDC/CI adapter of a connected DRM connector" in err
    assert rig.state()["reads"]["FD"] == reads, "nothing is read for a write that is refused anyway"
    assert rig.writes() == []


def test_dvi_writes_need_untested(rig):
    rig.use("PG39WCDM", connector="card1-DVI-D-1", bus=6)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "writes over DVI have not been verified on any monitor" in err
    assert rig.writes() == []


def test_an_hdmi_input_behind_a_dp_connector_needs_untested(rig):
    # An active DisplayPort-to-HDMI adapter or a dock: the GPU sees DisplayPort, the monitor HDMI.
    rig.set_reg("60", 0x11)
    code, out, _ = rig.run_cli("features")
    assert "the monitor's own input is HDMI (0x60 = 0x11) on a DisplayPort connector" in out
    assert "ready" not in out
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "writes over HDMI have not been verified on any monitor" in err
    assert rig.writes() == []
    code, _, err = rig.run_cli("oled", "uniform", "on", "--untested", "PG39WCDM")
    assert code == osdless.EXIT_OK, err
    assert "untested: the monitor's own input is HDMI" in err
    assert rig.writes() == [(0xFD, 0x69)]


def test_an_unreadable_or_unlisted_input_makes_a_dp_write_untested(rig):
    rig.set_state(fail_reads={"60": -1})
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "the monitor's own input (0x60) read failed (Maximum retries exceeded), so its link is unknown" in err
    rig.set_state(fail_reads={})
    rig.set_reg("60", 0x10)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "read 0x0010, so its link is unknown" in err
    rig.set_reg("60", 0x0F)
    rig.set_caps(" 60(0F 11 12 1A)", "")
    shutil.rmtree(rig.cache)
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "the capabilities do not list 0x60" in err
    assert rig.writes() == []


def test_the_input_is_read_once_and_only_for_a_write_that_could_go_ahead(rig):
    assert rig.run_cli("oled", "saver", "off")[0] == osdless.EXIT_REFUSED
    assert "60" not in rig.state()["reads"]
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_OK
    reads = [c["argv"][-1] for c in rig.calls() if c["command"] == "getvcp"]
    assert reads.count("0x60") == 1
    assert reads.index("0x60") < reads.index("0xFD")


def test_features_names_the_missing_serial_and_the_write_reads_nothing(rig):
    rig.set_state(displays=[{"bus": 9, "connector": "card1-DP-1", "monitor": "AUS:PG39WCDM:"}])
    code, out, _ = rig.run_cli("features")
    assert code == osdless.EXIT_OK
    line = next(line for line in out.splitlines() if "oled uniform" in line)
    assert "no serial number" in line
    assert rig.run_cli("oled", "uniform", "on")[0] == osdless.EXIT_REFUSED
    assert {"FD", "60"} & set(rig.state()["reads"]) == set()


def test_hdmi_monitors_are_read_through_the_ddc_symlink_and_written_only_with_untested(rig):
    rig.use("PG39WCDM", connector="card1-HDMI-A-1", bus=6)
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    assert "on /dev/i2c-6 (card1-HDMI-A-1, HDMI)" in out
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "writes over HDMI have not been verified on any monitor" in err
    assert rig.writes() == []
    code, _, err = rig.run_cli("oled", "uniform", "on", "--untested", "PG39WCDM")
    assert code == osdless.EXIT_OK, err
    assert "untested: writes over HDMI" in err
    assert rig.writes() == [(0xFD, 0x69)]


def test_capabilities_naming_another_model_make_the_monitor_read_only(rig):
    rig.set_caps("model(PG39WCDM)", "model(PG34WCDM)")
    code, out, _ = rig.run_cli("status")
    assert code == osdless.EXIT_OK
    assert "profile read-only: the capabilities name model 'PG34WCDM', not PG39WCDM" in out
    code, _, err = rig.run_cli("oled", "uniform", "on")
    assert code == osdless.EXIT_REFUSED
    assert "PG34WCDM" in err
    assert rig.writes() == []


def test_untested_must_name_the_chosen_monitor(rig):
    code, _, err = rig.run_cli("oled", "saver", "off", "--untested", "PG34WCDM")
    assert code == osdless.EXIT_REFUSED
    assert "--untested names PG34WCDM, but the chosen monitor is a PG39WCDM" in err
    assert rig.subcommands() == ["detect"]


def test_missing_ddcutil_is_a_read_failure(rig, monkeypatch):
    monkeypatch.setenv("OSDLESS_DDCUTIL", str(rig.root / "no-such-ddcutil"))
    assert rig.run_cli("status")[0] == osdless.EXIT_READ_FAILED


def test_connector_selectors_match_the_full_or_short_name():
    display = osdless.Display(9, "card1-DP-1", "AUS", "PG39WCDM", SERIAL)
    assert osdless.Selector(connector="DP-1").matches(display, "card1-DP-1")
    assert osdless.Selector(connector="card1-DP-1").matches(display, "card1-DP-1")
    assert not osdless.Selector(connector="DP-11").matches(display, "card1-DP-1")
    assert not osdless.Selector(connector="card0-DP-1").matches(display, "card1-DP-1")
    assert not osdless.Selector(connector="DP-1").matches(display, "")
