import hashlib
import json

import osdless
from rig import TOOL

SERIAL = "SYNTHETIC0001"


def report(rig, tmp_path, *argv):
    out_dir = tmp_path / "reports"
    out_dir.mkdir(exist_ok=True)
    code, out, err = rig.run_cli("report", "--output", str(out_dir), *argv)
    assert code == osdless.EXIT_OK, err
    (path,) = out_dir.glob("osdless-report-*.json")
    return path, out


def test_report_never_sends_setvcp(rig, tmp_path):
    report(rig, tmp_path)
    assert "setvcp" not in rig.subcommands()
    assert rig.writes() == []
    assert rig.helper_calls("logger") == []


def test_report_redacts_the_serial(rig, tmp_path):
    path, out = report(rig, tmp_path)
    assert SERIAL not in path.read_text()
    assert SERIAL not in out
    assert json.loads(path.read_text())["display"]["serial"] == "<redacted>"
    assert "The serial number is redacted" in out


def test_report_reads_each_listed_code_once_in_batches(rig, tmp_path):
    path, _ = report(rig, tmp_path)
    doc = json.loads(path.read_text())
    reads = [c["argv"] for c in rig.calls() if c["command"] == "getvcp"]
    bootstrap, sweep = reads[0], reads[1:]
    assert bootstrap[-2:] == ["0xFF", "0xEF"]
    swept = [arg for argv in sweep for arg in argv if arg.startswith("0x")]
    caps = osdless.parse_caps(rig.fixture["capabilities"])
    expected = [f"0x{code:02X}" for code in sorted(caps.vcp) if code not in osdless.NEVER_SEND]
    assert swept == expected
    assert all(len([a for a in argv if a.startswith("0x")]) <= osdless.REPORT_BATCH for argv in sweep)
    assert "0x04" not in swept and "0x08" not in swept
    assert "0xE8" not in swept and "0xEB" not in swept, "commands, not settings"
    assert doc["registers"] == rig.fixture["registers"]
    assert doc["read_errors"] == {}


def test_report_names_the_code_and_the_system(rig, tmp_path):
    path, out = report(rig, tmp_path)
    doc = json.loads(path.read_text())
    assert doc["schema"] == 1
    assert doc["osdless"] == {"version": osdless.__version__, "sha256": hashlib.sha256(TOOL.read_bytes()).hexdigest()}
    assert doc["ddcutil"] == "3.0.2"
    assert doc["system"]["gpu_driver"] == "amdgpu"
    assert doc["system"]["transport"] == "DisplayPort"
    assert doc["profile"]["name"] == "asus-v2-gaming"
    assert {row["command"]: row["status"] for row in doc["features"]} == rig.fixture["expect"]["features"]
    assert "| `oled uniform` | verified | on off | verified on the PG39WCDM |" in out


def test_a_report_loads_back_as_a_fixture(rig, tmp_path):
    status = json.loads(rig.run_cli("status", "--json")[1])
    path, _ = report(rig, tmp_path)
    fixture = json.loads(path.read_text())
    fixture["display"]["serial"] = SERIAL
    rig.use(fixture)
    replayed = json.loads(rig.run_cli("status", "--json")[1])
    assert replayed.pop("caps_source").startswith("cached in ")
    assert status.pop("caps_source") == "read live"
    assert replayed == status


def test_report_on_a_monitor_from_another_maker(rig, tmp_path):
    rig.use("generic-mccs")
    path, _ = report(rig, tmp_path)
    doc = json.loads(path.read_text())
    assert doc["profile"]["writes"] is False
    assert {row["status"] for row in doc["features"]} == {"unavailable"}
    assert "SYNTHETIC0008" not in path.read_text()
    assert path.name.startswith("osdless-report-GEN27-")


def test_report_json_prints_the_document(rig, tmp_path):
    path, out = report(rig, tmp_path, "--json")
    assert json.loads(out) == json.loads(path.read_text())


def test_report_needs_an_existing_directory(rig, tmp_path):
    code, _, err = rig.run_cli("report", "--output", str(tmp_path / "missing"))
    assert code == osdless.EXIT_REFUSED
    assert "is not a directory" in err
    assert rig.calls() == []


def test_report_into_a_directory_it_cannot_write_is_refused_first(rig, tmp_path):
    out_dir = tmp_path / "read-only"
    out_dir.mkdir()
    out_dir.chmod(0o500)
    try:
        proc = rig.run_process("report", "--output", str(out_dir), "--json")
    finally:
        out_dir.chmod(0o700)
    assert proc.returncode == osdless.EXIT_REFUSED, proc.stderr
    assert json.loads(proc.stdout)["error"] == f"cannot write the report into {out_dir}: permission denied"
    assert "Traceback" not in proc.stderr
    assert rig.calls() == []


def test_a_failed_report_write_is_refused(rig, tmp_path, monkeypatch):
    def full(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(osdless.Path, "write_text", full)
    code, out, err = rig.run_cli("report", "--output", str(tmp_path), "--json")
    assert code == osdless.EXIT_REFUSED
    assert "cannot write the report to" in err
    assert json.loads(out)["error"].endswith(": No space left on device")
