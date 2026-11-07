import json

import pytest

import osdless
from rig import fixture_names, load_fixture

PG39 = osdless.Display(9, "card1-DP-1", "AUS", "PG39WCDM", "SYNTHETIC0001")
CAPS = osdless.parse_caps(load_fixture("PG39WCDM")["capabilities"])
EF = osdless.Reading(0xEF, 0x02, 0x07, 0x00, 0xB1)


@pytest.mark.parametrize("name", fixture_names())
def test_each_fixture_gets_its_expected_profile_and_features(rig, name):
    rig.use(name)
    code, out, err = rig.run_cli("features", "--json")
    assert code == osdless.EXIT_OK, err
    doc = json.loads(out)
    expect = rig.fixture["expect"]
    assert doc["profile"]["name"] == expect["profile"]
    assert {row["command"]: row["status"] for row in doc["features"]} == expect["features"]
    assert rig.writes() == []


def test_the_pg39wcdm_profile_and_its_reasons():
    profile, reasons = osdless.choose_profile(PG39, CAPS, EF)
    assert profile is osdless.ASUS_V2_GAMING
    assert reasons == (
        "EDID maker AUS",
        "0xEF support code 0xB1, VCP table 2.7",
        "PG39WCDM is in the ASUS gaming line",
        "the capabilities name the same model",
    )


@pytest.mark.parametrize(
    ("display", "caps", "ef", "reason"),
    [
        (osdless.Display(9, "", "XYZ", "GEN27", "1"), CAPS, EF, "EDID maker XYZ is not AUS"),
        (PG39, osdless.parse_caps("(model(PG39WCDM) vcp(10 FD(00FD)))"), None, "do not list 0xEF"),
        (PG39, CAPS, "Maximum retries exceeded", "reading 0xEF failed: Maximum retries exceeded"),
        (PG39, CAPS, osdless.Reading(0xEF, 0, 0xFE, 0, 0xFE), "not an ASUS support code"),
        (PG39, CAPS, osdless.Reading(0xEF, 0x02, 0x07, 0x00, 0x0A), "0xEF reads 0x000A"),
        (PG39, CAPS, osdless.Reading(0xEF, 0x02, 0x07, 0x01, 0xB1), "0xEF reads 0x01B1"),
        (PG39, CAPS, osdless.Reading(0xEF, 0x01, 0x07, 0x00, 0xB1), "VCP table 1.7 is not table v2"),
        (PG39, CAPS, osdless.Reading(0xEF, 0x03, 0x00, 0x00, 0xB1), "VCP table 3.0 is not table v2"),
        (osdless.Display(9, "", "AUS", "PA32UCDM", "1"), CAPS, EF, "PA32UCDM is not in the ASUS gaming line"),
        (osdless.Display(9, "", "AUS", "VA27EHE", "1"), CAPS, EF, "VA27EHE is not in the ASUS gaming line"),
        (osdless.Display(9, "", "AUS", "PG34WCDM", "1"), CAPS, EF, "the capabilities name model 'PG39WCDM'"),
    ],
)
def test_anything_else_is_read_only_and_says_why(display, caps, ef, reason):
    profile, reasons = osdless.choose_profile(display, caps, ef)
    assert profile is osdless.READ_ONLY
    assert reason in reasons[-1]


def test_the_asus_support_codes():
    codes = osdless.ASUS_SUPPORT_CODES
    assert len(codes) == 45
    for code in (0x1A, 0x55, 0x58, 0xB1, 0xB4, 0xF5, 0xF8):
        assert code in codes
    for code in (0x00, 0x0A, 0x54, 0x59, 0xB5, 0xF9, 0xFE, 0x1B1):
        assert code not in codes


def test_gaming_models_from_other_tables_or_lines_still_decode_mccs(rig):
    rig.use("ProArt")
    code, out, err = rig.run_cli("status")
    assert code == osdless.EXIT_OK, err
    assert "profile read-only: PA32UCDM is not in the ASUS gaming line" in out
    assert "OLED care" not in out
    assert "Colour preset" in out


def test_a_gaming_lcd_on_table_2_is_offered_no_oled_write(rig):
    lcd = load_fixture("PG39WCDM")
    lcd["display"]["model"] = "VG27AQ3A"
    lcd["display"]["serial"] = "SYNTHETIC0099"
    lcd["capabilities"] = lcd["capabilities"].replace("model(PG39WCDM)", "model(VG27AQ3A)")
    rig.use(lcd)
    doc = json.loads(rig.run_cli("features", "--json")[1])
    assert doc["profile"]["name"] == "asus-v2-gaming"
    rows = {row["command"]: row for row in doc["features"]}
    for name, row in rows.items():
        if name.startswith("oled "):
            assert row["status"] == "unavailable", name
            assert row["reason"].startswith("VG27AQ3A is not an OLED by ASUS's naming rule"), name
        else:
            assert row["status"] == "untested", name
    for argv in (("oled", "uniform", "on"), ("oled", "clean", "--yes"), ("oled", "reminder", "2")):
        code, _, err = rig.run_cli(*argv, "--untested", "VG27AQ3A")
        assert code == osdless.EXIT_REFUSED, argv
        assert "VG27AQ3A is not an OLED" in err
    assert {"FD", "F8", "D6"} & set(rig.state()["reads"]) == set()
    assert rig.helper_calls("systemd-inhibit") == []
    assert rig.writes() == []
    assert rig.run_cli("power", "standard", "--untested", "VG27AQ3A")[0] == osdless.EXIT_OK
    assert rig.writes() == [(0xE1, 0)]
