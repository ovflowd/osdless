import ast
import sys
import tomllib

import osdless
from rig import TOOL


def test_pep_723_header():
    head = TOOL.read_text().splitlines()[:5]
    assert head[0] == "#!/usr/bin/env python3"
    assert head[1:] == ["# /// script", '# requires-python = ">=3.11"', "# dependencies = []", "# ///"]


def test_standard_library_only():
    tree = ast.parse(TOOL.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names)


def test_the_version_is_written_once_per_file_and_agrees():
    project = tomllib.loads((TOOL.parent / "pyproject.toml").read_text())["project"]
    assert project["version"] == osdless.__version__
    assert project["requires-python"] == ">=3.11"
    assert project["dependencies"] == []
