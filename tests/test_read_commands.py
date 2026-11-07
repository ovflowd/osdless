import itertools
import json
import stat

import osdless

SERIAL = "SYNTHETIC0001"


def test_status_decodes_the_scan_snapshot(rig):
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    for text in (
        f"AUS:PG39WCDM:{SERIAL} on /dev/i2c-9 (card1-DP-1, DisplayPort); capabilities read live",
        "profile asus-v2-gaming",
        "40 / 100 (never written: the desktop owns brightness)",
        "6500 K",
        "DisplayPort",
        "Gamma              2.2",
        "Racing",
        "Power setting      Power Saving",
        "HDR                off",
        "KVM                USB-B upstream",
        "every 8 h",
        "Screen move        strong",
        "shortcut 1 GameVisual, shortcut 2 GamePlus",
        "auto input detection on",
        "(read-only)",
        "VRR on",
        "screen saver on",
        "pixel cleaning idle",
        "logo brightness on",
        "uniform brightness off",
        "clear pixel edge off",
        "version 103, panel 0x20",
        "VCP table 2.7 (new)",
    ):
        assert text in out
    assert "ASCR" not in out, "0x0002 is outside the FD caps mask"
    assert rig.writes() == []


def test_status_json(rig):
    code, out, err = rig.run_cli("status", "--json")
    assert code == osdless.EXIT_OK, err
    doc = json.loads(out)
    assert doc["display"] == {
        "mfg": "AUS",
        "model": "PG39WCDM",
        "serial": SERIAL,
        "bus": 9,
        "connector": "card1-DP-1",
        "transport": "DisplayPort",
    }
    assert doc["profile"]["name"] == "asus-v2-gaming"
    assert doc["profile"]["writes"] is True
    assert doc["profile"]["reasons"][-1] == "the capabilities name the same model"
    assert doc["vcp_table"] == "2.7"
    assert doc["codes"]["0xE1"]["text"] == "Power Saving"
    assert doc["codes"]["0xFD"]["cur"] == 0x29
    assert doc["codes"]["0xFD"]["raw"] == {"mh": 0, "ml": 0xFD, "sh": 0, "sl": 0x29}
    assert doc["codes"]["0xFD"]["bits"] == {
        "VRR": True,
        "Sniper": False,
        "screen saver": True,
        "pixel cleaning running": False,
        "logo brightness": True,
        "uniform brightness": False,
        "clear pixel edge": False,
    }
    assert sorted(doc["codes"]) == sorted(f"0x{entry.code:02X}" for entry in osdless.ASUS_STATUS)


def test_status_caches_capabilities_per_firmware_and_vcp_id(rig):
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.subcommands().count("capabilities") == 1
    cache = rig.cache / "osdless"
    assert sorted(p.name for p in cache.iterdir()) == [f"caps-AUS-PG39WCDM-{SERIAL}-ff0067-ef00B10207.json"]
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700, "the cache files hold the serial"
    rig.set_reg("FF", 0x20, 0x68)
    code, out, _ = rig.run_cli("status")
    assert code == osdless.EXIT_OK
    assert rig.subcommands().count("capabilities") == 2
    assert "version 104" in out
    caps_call = next(c for c in rig.calls() if c["command"] == "capabilities")
    assert "--disable-capabilities-cache" in caps_call["argv"]


def test_corrupt_cache_is_read_again(rig):
    rig.run_cli("status")
    cached = next((rig.cache / "osdless").iterdir())
    good = json.loads(cached.read_text())
    variants = [
        "{not json",
        "[]",
        json.dumps({**good, "serial": "SYNTHETIC9999"}),
        json.dumps({**good, "model": "PG34WCDM"}),
        json.dumps({**good, "ff_max": 0x68}),
        json.dumps({**good, "ef": [0xB1, 0x0107]}),
        json.dumps({**good, "capabilities": 7}),
        json.dumps({**good, "capabilities": "(model(PG39WCDM) vcp(10 1Z))"}),
    ]
    for garbage in variants:
        cached.write_text(garbage)
        assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.subcommands().count("capabilities") == 1 + len(variants)


def test_an_implausible_0xef_reply_does_not_key_the_cache(rig):
    rig.set_reg("EF", 0xFE, 0xFE)
    code, out, _ = rig.run_cli("status")
    assert code == osdless.EXIT_OK
    assert "profile read-only: 0xEF reads 0x00FE (max 0x00FE), not an ASUS support code" in out
    assert not (rig.cache / "osdless").exists()


def test_other_models_are_not_cached_and_read_0xef_after_the_capabilities(rig):
    rig.use("PG34WCDM")
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    assert rig.subcommands().count("capabilities") == 2
    assert not (rig.cache / "osdless").exists()
    calls = rig.calls()[:3]
    assert [c["command"] for c in calls] == ["detect", "capabilities", "getvcp"]
    assert calls[2]["argv"][-1] == "0xEF"


def test_monitors_from_other_makers_are_never_read_before_their_capabilities(rig):
    rig.use("generic-mccs")
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    assert rig.subcommands() == ["detect", "capabilities", "getvcp"]
    assert "0xEF" not in rig.calls()[2]["argv"]
    assert "profile read-only: EDID maker XYZ is not AUS" in out
    for text in ("Colour preset      sRGB", "Input              DisplayPort-1", "Power mode         on"):
        assert text in out
    assert "OLED care" not in out


def test_status_shows_the_not_available_marker(rig):
    rig.set_reg("F8", 0xFE, 0xFE)
    code, out, _ = rig.run_cli("status")
    assert code == osdless.EXIT_OK
    assert "0xF8  Cleaning reminder  n/a in this mode" in out


def test_status_with_a_failed_read_exits_3_and_shows_the_rest(rig):
    rig.set_state(fail_reads={"E2": -1})
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_READ_FAILED
    assert "0xE2  HDR                read failed: Maximum retries exceeded" in out
    assert "Power Saving" in out
    assert "0xE2" in err


def test_every_monitor_call_uses_the_chosen_displays_bus(rig):
    rig.use("PG39WCDM", connector="card1-DP-2", bus=12)
    assert rig.run_cli("status")[0] == osdless.EXIT_OK
    for call in rig.calls():
        if call["command"] == "detect":
            continue
        argv = call["argv"]
        assert argv[argv.index("--bus") + 1] == "12"


def test_ddcutil_calls_are_spaced(rig):
    rig.run_cli("status")
    calls = rig.calls()
    for before, after in itertools.pairwise(calls):
        assert after["start"] - before["end"] >= osdless.MIN_GAP * 0.9


def test_get_prints_the_raw_reply(rig):
    code, out, _ = rig.run_cli("get", "fd")
    assert code == osdless.EXIT_OK
    assert out.strip() == "0xFD: cur=0x0029 (41) max=0x00FD (253) mh=00 ml=FD sh=00 sl=29  caps mask 0x00FD"


def test_get_json(rig):
    code, out, _ = rig.run_cli("get", "0xF8", "--json")
    assert code == osdless.EXIT_OK
    assert json.loads(out)["caps_values"] == [0, 2, 4, 8]


def test_get_shows_the_not_available_marker(rig):
    rig.set_reg("F8", 0xFE, 0xFE)
    code, out, _ = rig.run_cli("get", "F8")
    assert code == osdless.EXIT_OK
    assert "n/a in this mode" in out


def test_get_works_on_any_monitor(rig):
    rig.use("generic-mccs")
    code, out, _ = rig.run_cli("get", "60")
    assert code == osdless.EXIT_OK
    assert out.strip() == "0x60: cur=0x000F (15) max=0x0012 (18) mh=00 ml=12 sh=00 sl=0F  caps (0F 11 12)"


def test_never_send_codes_are_refused_before_any_traffic(rig):
    for text in ("FE", "0xfe", "dd", "0xDD", "04", "4", "08", "EC", "01", "05", "06", "0A", "B0", "E8", "eb"):
        assert rig.run_cli("get", text)[0] == osdless.EXIT_REFUSED
    assert rig.calls() == []


def test_codes_outside_the_capabilities_are_refused(rig):
    for text in ("45", "ED", "C5", "79"):
        assert rig.run_cli("get", text)[0] == osdless.EXIT_REFUSED
    reads = rig.state()["reads"]
    for code in ("45", "ED", "C5", "79"):
        assert code not in reads


def test_malformed_codes_are_refused(rig):
    for text in ("zz", "100", "0x", "-1"):
        assert rig.run_cli("get", text)[0] == osdless.EXIT_REFUSED
    assert rig.calls() == []


def test_failed_read_exits_3(rig):
    rig.set_state(fail_reads={"FD": -1})
    assert rig.run_cli("get", "FD")[0] == osdless.EXIT_READ_FAILED


def test_unsupported_reply_exits_3(rig):
    rig.mutate(lambda state: state["regs"].pop("87"))
    rig.set_state(read_errors={"87": "Unsupported feature code"})
    code, _, err = rig.run_cli("get", "87")
    assert code == osdless.EXIT_READ_FAILED
    assert "Unsupported feature code" in err


def test_list_shows_every_valid_display(rig):
    rig.set_state(
        displays=[
            {"bus": 9, "connector": "card1-DP-1", "monitor": f"AUS:PG39WCDM:{SERIAL}"},
            {"bus": 10, "monitor": f"AUS:PG39WCDM:{SERIAL}", "invalid": True},
            {"bus": 14, "monitor": "XYZ:GEN27:SYNTHETIC0008"},
        ]
    )
    code, out, _ = rig.run_cli("list")
    assert code == osdless.EXIT_OK
    assert out.splitlines() == [
        f"/dev/i2c-9   AUS:PG39WCDM:{SERIAL} (card1-DP-1, DisplayPort)",
        "/dev/i2c-14  XYZ:GEN27:SYNTHETIC0008 (not mapped to a DRM connector: read-only)",
    ]
    assert rig.subcommands() == ["detect"]
    doc = json.loads(rig.run_cli("list", "--json")[1])
    assert [d["bus"] for d in doc["displays"]] == [9, 14]
    assert doc["displays"][1]["transport"] is None


def test_list_with_no_display_exits_3(rig):
    rig.set_state(detect_fail=True)
    code, _, err = rig.run_cli("list")
    assert code == osdless.EXIT_READ_FAILED
    assert "no DDC/CI display" in err


def test_features_on_the_pg39wcdm(rig):
    code, out, _ = rig.run_cli("features")
    assert code == osdless.EXIT_OK
    lines = {line.split()[0] + " " + line.split()[1]: line for line in out.splitlines() if line.startswith("  oled")}
    assert lines["oled uniform"].rstrip().endswith("ready")
    for name in ("oled saver", "oled logo", "oled clear-edge", "oled reminder", "oled screen-move"):
        assert "needs --untested PG39WCDM: " + name + " has not been verified on the PG39WCDM" in lines[name]
    assert "0xEF support code 0xB1, VCP table 2.7" in out
    assert rig.subcommands() == ["detect", "getvcp", "capabilities", "getvcp"]
    assert rig.calls()[-1]["argv"][-1] == "0x60"
    assert rig.writes() == []


def test_features_json_lists_the_values_the_monitor_offers(rig):
    rig.use("PG34WCDM")
    doc = json.loads(rig.run_cli("features", "--json")[1])
    rows = {row["command"]: row for row in doc["features"]}
    assert rows["kvm"]["values"] == ["usb-b", "usb-c"]
    assert rows["input"]["values"] == ["dp", "hdmi1", "hdmi2", "usbc"]
    assert rows["oled uniform"] == {
        "command": "oled uniform",
        "code": "0xFD",
        "bit": "0x0040",
        "write": "bit",
        "status": "untested",
        "values": ["on", "off"],
        "reason": "oled uniform has not been verified on the PG34WCDM",
    }


def test_ddcutil_failing_is_not_reported_as_a_missing_monitor(rig, monkeypatch):
    old = rig.root / "old-ddcutil"
    old.write_text("#!/bin/sh\necho \"ddcutil: unrecognized option '--enable-cross-instance-locks'\"\nexit 1\n")
    old.chmod(0o755)
    monkeypatch.setenv("OSDLESS_DDCUTIL", str(old))
    for command in ("list", "status"):
        code, _, err = rig.run_cli(command)
        assert code == osdless.EXIT_READ_FAILED
        assert "ddcutil detect failed (exit 1: ddcutil: unrecognized option '--enable-cross-instance-locks')" in err
        assert "is the monitor on" not in err


def test_a_failed_capabilities_read_shows_what_ddcutil_said(rig):
    rig.set_state(caps_fail=True)
    code, _, err = rig.run_cli("status")
    assert code == osdless.EXIT_READ_FAILED
    assert "ddcutil capabilities failed (exit 1: Unable to get capabilities for monitor on /dev/i2c-9)" in err


def test_status_names_every_bit_in_the_mask(rig):
    rig.use("XG27ACDNG")
    code, out, err = rig.run_cli("status", "--json")
    assert code == osdless.EXIT_OK, err
    doc = json.loads(out)
    assert doc["codes"]["0xFD"]["bits"]["unidentified 0x0800"] is True
    assert list(doc["codes"]["0xFC"]["bits"])[-1] == "unidentified 0x0100"
    code, out, _ = rig.run_cli("status")
    assert "uniform brightness off, unidentified 0x0800 on, unidentified 0x1000 off" in out
