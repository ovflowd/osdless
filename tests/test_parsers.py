import pytest

import osdless
from rig import fixture_names, load_fixture

SCAN_GETVCP = """\
Getting data for non-table VCP code 0xc0 - Display usage time:
VCP code 0xc0 (Display usage time            ): Unsupported feature code
Getting data for non-table VCP code 0xfd - Manufacturer Specific:
Raw value: opcode=0xfd, mh=0x00, ml=0xfd, sh=0x00, sl=0x29, max_val=253 (0x00fd), cur_val=41 (0x0029)
VCP code 0xfd (Manufacturer Specific         ): mh=0x00, ml=0xfd, sh=0x00, sl=0x29
Getting data for non-table VCP code 0xfe - Manufacturer Specific:
VCP code 0xfe (Manufacturer Specific         ): Maximum retries exceeded
Getting data for non-table VCP code 0x14 - Select color preset:
Raw value: opcode=0x14, mh=0x00, ml=0x0b, sh=0x00, sl=0x05, max_val=11 (0x000b), cur_val=5 (0x0005)
VCP code 0x14 (Select color preset           ): 6500 K (0x05), Tolerance: Unspecified (0x00)
"""

SCAN_DETECT = """\
Display 1
   I2C bus:          /dev/i2c-9
   DRM connector:    card1-DP-1
   drm_connector_id: 458
   Monitor:          AUS:PG39WCDM:SYNTHETIC0001

Invalid display
   I2C bus:          /dev/i2c-10
   Monitor:          AUS:PG39WCDM:SYNTHETIC0001
"""


def test_live_capabilities():
    caps = osdless.parse_caps(load_fixture("PG39WCDM")["capabilities"])
    assert caps.model == "PG39WCDM"
    assert caps.mccs_version == "2.0"
    assert caps.mask(0xFD) == 0x00FD
    assert caps.mask(0xFC) == 0x049F
    assert caps.values(0xF8) == (0, 2, 4, 8)
    assert caps.values(0x60) == (0x0F, 0x11, 0x12, 0x1A)
    assert caps.values(0xE1) == (0, 1)
    assert caps.values(0xF3) == (1, 2, 3)
    assert caps.has(0xEF) and caps.has(0xFF) and caps.has(0xD6)
    assert not (caps.has(0xFE) or caps.has(0xDD) or caps.has(0xED) or caps.has(0xC5))


def test_lowercase_codes_parse():
    caps = osdless.parse_caps("(model(PG39WCDM) vcp(e3(07 0a) fd(00fd)))")
    assert caps.values(0xE3) == (0x07, 0x0A)
    assert caps.mask(0xFD) == 0xFD


def test_four_digit_values_parse():
    assert osdless.parse_caps(load_fixture("XG27ACDNG")["capabilities"]).mask(0xFD) == 0x7879
    proart = osdless.parse_caps(load_fixture("ProArt")["capabilities"])
    assert proart.model == "ProArtDisplay"
    assert proart.mask(0xFD) == 0x1A05


def test_a_mask_needs_exactly_one_value():
    caps = osdless.parse_caps("(model(X) vcp(FC FD(00FD 00FE)))")
    assert caps.mask(0xFC) == 0
    assert caps.mask(0xFD) == 0


@pytest.mark.parametrize("name", fixture_names())
def test_every_fixture_capabilities_string_parses(name):
    data = load_fixture(name)
    caps = osdless.parse_caps(data["capabilities"])
    assert caps.vcp
    assert set(data["registers"]) | set(data["read_errors"]) == {
        f"{code:02X}" for code in caps.vcp if code not in osdless.NEVER_SEND
    }


def test_malformed_capabilities_are_rejected():
    for raw in (
        "(model(X) vcp(10 1Z))",
        "(model(X) vcp(10 12",
        "(vcp(10))",
        "(model(X) vcp(123))",
        "(model(X) vcp((1)))",
    ):
        with pytest.raises(osdless.ReadFailed):
            osdless.parse_caps(raw)


def test_getvcp_output_from_the_scan():
    results = osdless.parse_getvcp(SCAN_GETVCP, [0xFD, 0xC0, 0xFE, 0x14, 0x62])
    assert results[0xFD] == osdless.Reading(0xFD, 0x00, 0xFD, 0x00, 0x29)
    assert results[0xC0] == "Unsupported feature code"
    assert results[0xFE] == "Maximum retries exceeded"
    fourteen = results[0x14]
    assert isinstance(fourteen, osdless.Reading) and fourteen.cur == 5
    assert results[0x62] == "no reply"


def test_duplicate_replies_are_not_trusted():
    doubled = SCAN_GETVCP + SCAN_GETVCP.split("\n", 3)[3]
    assert osdless.parse_getvcp(doubled, [0xFD])[0xFD] == "more than one reply"


def test_replies_for_codes_not_asked_are_ignored():
    assert osdless.parse_getvcp(SCAN_GETVCP, [0x62]) == {0x62: "no reply"}


def test_not_available_marker():
    assert osdless.Reading(0xF8, 0, 0xFE, 0, 0xFE).not_available
    assert not osdless.Reading(0xE2, 0, 0xFE, 0, 0).not_available


def test_detect_output():
    assert osdless.parse_detect(SCAN_DETECT) == [osdless.Display(9, "card1-DP-1", "AUS", "PG39WCDM", "SYNTHETIC0001")]


def test_detect_output_with_several_displays():
    output = (
        SCAN_DETECT
        + "\nDisplay 2\n   I2C bus:          /dev/i2c-6\n   Monitor:          XYZ:Office 27:\n"
        + "\nPhantom display\n   I2C bus:          /dev/i2c-7\n   Monitor:          AUS:PG39WCDM:SYNTHETIC0001\n"
        + "\nDisplay 3\n   I2C bus:          /dev/i2c-11\n   DRM connector:    card1-DP-3\n"
    )
    assert osdless.parse_detect(output) == [
        osdless.Display(9, "card1-DP-1", "AUS", "PG39WCDM", "SYNTHETIC0001"),
        osdless.Display(6, "", "XYZ", "Office 27", ""),
    ]


def test_vcp_codes_parse():
    assert [osdless.parse_code(text) for text in ("FD", "0xfd", "0XFD", " 4 ", "e1")] == [0xFD, 0xFD, 0xFD, 4, 0xE1]
    for text in ("zz", "100", "0x", "-1", ""):
        with pytest.raises(osdless.Refused):
            osdless.parse_code(text)
