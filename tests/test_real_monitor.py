"""Read-only checks against a real monitor, which run only when asked for.

OSDLESS_REAL_MONITOR=1 uv run pytest tests/test_real_monitor.py
OSDLESS_REAL_MONITOR=1 OSDLESS_REAL_SELECTOR="--model PG39WCDM" uv run pytest tests/test_real_monitor.py

Both commands use a runner that cannot send setvcp. The report lands in pytest's temporary
directory, never in the repository.
"""

import json
import os
import shlex
import subprocess
import sys

import pytest

from rig import TOOL

pytestmark = pytest.mark.skipif(os.environ.get("OSDLESS_REAL_MONITOR") != "1", reason="no real monitor requested")
SELECTOR = shlex.split(os.environ.get("OSDLESS_REAL_SELECTOR", ""))


def run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", str(TOOL), *SELECTOR, *argv],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_status_reads_the_real_monitor():
    proc = run("status", "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["profile"]["reasons"]


def test_report_reads_the_real_monitor_without_the_serial(tmp_path):
    proc = run("report", "--output", str(tmp_path), "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["display"]["serial"] == "<redacted>"
    assert doc["registers"]
