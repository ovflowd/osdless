import argparse
import re

import osdless
from rig import TOOL

README = (TOOL.parent / "README.md").read_text()
MATRIX = (TOOL.parent / "docs" / "supported-monitors.md").read_text()


def table(section: str, text: str = README) -> tuple[list[str], list[list[str]]]:
    lines = text.split(f"\n## {section}\n", 1)[1].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip("|"))])
    return rows[0], rows[2:]


def parsers(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    found = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                found += parsers(child)
    return found


def test_the_support_matrix_marks_exactly_the_verified_writes():
    header, rows = table("Support matrix", MATRIX)
    names = {feature.name for feature in osdless.FEATURES}
    marked = set()
    for row in rows:
        feature = row[0].strip("`")
        if feature in names:
            marked |= {(model, feature) for model, cell in zip(header[1:], row[1:], strict=True) if "VERIFIED" in cell}
    assert marked == osdless.VERIFIED
    assert names <= {row[0].strip("`") for row in rows}


def test_the_readme_marks_tested_only_the_models_with_a_verified_write():
    _, rows = table("Supported monitors")
    tested = {row[0].split()[-1] for row in rows if row[1] == "Tested"}
    assert tested == {model for model, _ in osdless.VERIFIED}


def test_the_command_table_lists_every_command_and_its_values():
    _, rows = table("Usage")
    commands = [row[0].strip("`").split() for row in rows]
    parser = osdless.build_parser()
    top = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert set(top.choices) <= {words[0] for words in commands}
    for feature in osdless.FEATURES:
        *path, leaf = feature.command
        row = next(words for words in commands if words[: len(path)] == path and leaf in words[len(path)].split("|"))
        if feature.write is osdless.Write.BIT:
            assert row[len(feature.command)] == "on|off"
        elif feature.choices:
            assert row[len(feature.command)] == "|".join(word for word, _ in feature.choices)


def test_every_option_is_documented():
    options = {
        option
        for parser in parsers(osdless.build_parser())
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    assert {option for option in options if f"`{option}" not in README} == set()
