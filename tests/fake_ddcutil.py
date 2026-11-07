#!/usr/bin/env python3
"""A fake ddcutil 3.0.2 for osdless's tests.

It replays one monitor from a JSON state file named by FAKE_DDC_STATE: the `detect` entries, the
capabilities string, the registers and the read errors (the shape of an `osdless report`), plus
scenario switches for read failures, writes that do not stick, registers changed by someone else
and slow replies. The output formats are copied from real ddcutil 3.0.2 runs.

Every call is logged to FAKE_DDC_STATE + ".calls" (JSON lines) as soon as it starts and again when
it ends, so calls killed by osdless's timeout still show up. Calls without the options that keep a
user's ddcutil configuration out are refused, and they, like anything osdless must never send, land
in the state's "violations" list. The lists here are written out on their own rather than imported,
so a mistake in osdless's lists cannot hide here too.
"""

import fcntl
import json
import os
import sys
import time
from typing import Any

REQUIRED = ("--noconfig", "--disable-udf", "--enable-cross-instance-locks")
FORBIDDEN = {"--disable-flock", "--disable-cross-instance-locks", "--enable-udf", "--force"}
NEVER_SEND = {0x01, 0x04, 0x05, 0x06, 0x08, 0x0A, 0xB0, 0xEC, 0xFE, 0xDD, 0xE8, 0xEB}
NEVER_WRITE = NEVER_SEND | {0x10, 0xFC, 0xE2, 0xC5, 0xD6, 0xED, 0x47, 0xF2}
NAMES = {
    0x02: "New control value",
    0x10: "Brightness",
    0x12: "Contrast",
    0x14: "Select color preset",
    0x60: "Input Source",
    0x62: "Audio speaker volume",
    0x72: "Gamma",
    0xD6: "Power mode",
    0xDC: "Display Mode",
}


def name_of(code: int) -> str:
    return NAMES.get(code, "Manufacturer Specific" if code >= 0xE0 else "Unknown feature")


def log_call(path: str, entry: dict[str, Any]) -> None:
    with open(path + ".calls", "a") as f:
        f.write(json.dumps(entry) + "\n")


def split_args(argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    options: dict[str, Any] = {}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            if "=" in token:
                key, value = token.split("=", 1)
                options[key] = value
            elif token == "--bus":
                options[token] = argv[i + 1]
                i += 1
            else:
                options[token] = True
        else:
            positional.append(token)
        i += 1
    return options, positional


def countdown(table: dict[str, int], key: str) -> bool:
    """True if the scenario table says this event should misbehave; -1 means always."""
    left = table.get(key, 0)
    if left == 0:
        return False
    if left > 0:
        table[key] = left - 1
    return True


def detect(state: dict[str, Any], out: list[str]) -> int:
    if state.get("detect_delay"):
        time.sleep(state["detect_delay"])
    if state.get("detect_fail"):
        out.append("No displays found.")
        return 0
    for n, display in enumerate(state["displays"], 1):
        out.append("Invalid display" if display.get("invalid") else f"Display {n}")
        out.append(f"   I2C bus:          /dev/i2c-{display['bus']}")
        if display.get("connector"):
            out.append(f"   DRM connector:    {display['connector']}")
            out.append("   drm_connector_id: 458")
        out.append(f"   Monitor:          {display['monitor']}")
        out.append("")
    return 0


def check_bus(state: dict[str, Any], options: dict[str, Any], out: list[str]) -> bool:
    if str(options.get("--bus")) != str(state["bus"]):
        out.append(f"Display not found on bus {options.get('--bus')}")
        return False
    return True


def capabilities(state: dict[str, Any], options: dict[str, Any], out: list[str]) -> int:
    if not check_bus(state, options, out):
        return 1
    if state.get("caps_delay"):
        time.sleep(state["caps_delay"])
    if state.get("caps_fail"):
        out.append(f"Unable to get capabilities for monitor on /dev/i2c-{state['bus']}")
        return 1
    out.append(f"Unparsed capabilities string: {state['caps']}")
    out.append("Errors parsing capabilities string:")
    out.append("   Invalid VCP value in list for feature xfd: 00FD")
    out.append("MCCS version: 2.0")
    out.append("   Feature: FD (Manufacturer specific feature)")
    out.append("      Values (unparsed): 00FD")
    out.append("      Values (  parsed): \udcff\udcfe#U (interpretation unavailable)")
    return 0


def getvcp(state: dict[str, Any], options: dict[str, Any], codes: list[str], out: list[str]) -> int:
    if not check_bus(state, options, out):
        return 1
    if state.get("getvcp_delay"):
        time.sleep(state["getvcp_delay"])
    rc = 0
    for token in sorted(codes, key=lambda t: int(t, 16)):
        code = int(token, 16)
        key = f"{code:02X}"
        name = name_of(code)
        if code in NEVER_SEND:
            state["violations"].append(f"getvcp 0x{key}")
        state["reads"][key] = state["reads"].get(key, 0) + 1
        out.append(f"Getting data for non-table VCP code 0x{code:02x} - {name}:")
        nth_fails = state["reads"][key] in state.get("fail_read_numbers", {}).get(key, [])
        if countdown(state["fail_reads"], key) or nth_fails:
            out.append(f"VCP code 0x{code:02x} ({name:<30}): Maximum retries exceeded")
            rc = 1
            continue
        reg = state["regs"].get(key)
        if reg is None:
            reason = state.get("read_errors", {}).get(key, "Unsupported feature code")
            out.append(f"VCP code 0x{code:02x} ({name:<30}): {reason}")
            rc = 1
            continue
        if key == "FD" and state.get("cleaning_reads_left") is not None:
            if state["cleaning_reads_left"] == 0:
                if state.get("clean_end_reg"):
                    reg[:] = state["clean_end_reg"]
                else:
                    reg[0] &= ~0x10
                state["cleaning_reads_left"] = None
            else:
                state["cleaning_reads_left"] -= 1
        cur, mx = reg
        out.append(
            f"Raw value: opcode=0x{code:02x}, mh=0x{mx >> 8:02x}, ml=0x{mx & 0xFF:02x}, sh=0x{cur >> 8:02x}, "
            f"sl=0x{cur & 0xFF:02x}, max_val={mx} (0x{mx:04x}), cur_val={cur} (0x{cur:04x})"
        )
        out.append(
            f"VCP code 0x{code:02x} ({name:<30}): mh=0x{mx >> 8:02x}, ml=0x{mx & 0xFF:02x}, "
            f"sh=0x{cur >> 8:02x}, sl=0x{cur & 0xFF:02x}"
        )
    return rc


def setvcp(state: dict[str, Any], options: dict[str, Any], args: list[str], out: list[str]) -> int:
    if not check_bus(state, options, out):
        return 1
    if len(args) != 2 or args[1] in ("+", "-"):
        state["violations"].append(f"setvcp with arguments {args}")
        return 1
    code, value = int(args[0], 16), int(args[1], 0)
    key = f"{code:02X}"
    state["writes"].append([code, value])
    state["write_args"].append(args[1])
    if code in NEVER_WRITE:
        state["violations"].append(f"setvcp 0x{key}")
    # ddcutil 3.0.2's man page: the value "is actually a two byte value, i.e. x00..xffff".
    if value > 0xFFFF:
        out.append(f"Invalid VCP value: {args[1]}")
        return 1
    if state.get("setvcp_delay"):
        time.sleep(state["setvcp_delay"])
    # Someone else (the OSD, the monitor itself) changing the register as this write lands.
    after = state.get("set_after_write", {}).pop(key, None)
    if after is not None:
        state["regs"][key] = after
    if countdown(state["ignore_writes"], key):
        return 0
    reg = state["regs"].get(key)
    if reg is None:
        out.append(f"Setting value failed for feature x{code:02x}")
        return 1
    reg[0] = value ^ state.get("write_side_effect_xor", {}).get(key, 0)
    if key == "FD" and value & 0x10 and state.get("clean_clear_after") is not None:
        state["cleaning_reads_left"] = state["clean_clear_after"]
    return 0


def main(argv: list[str]) -> int:
    path = os.environ["FAKE_DDC_STATE"]
    call_id = f"{os.getpid()}-{time.time_ns()}"
    options, positional = split_args(argv)
    command = positional[0] if positional else ("--version" if "--version" in options else "")
    log_call(path, {"id": call_id, "argv": argv, "command": command, "start": time.time()})
    out: list[str] = []
    # Stands in for ddcutil's own cross-instance lock, and keeps the state file consistent.
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(path) as f:
            state = json.load(f)
        for flag in sorted(FORBIDDEN & set(options)):
            state["violations"].append(f"flag {flag}")
        missing = [flag for flag in REQUIRED if flag not in options]
        args = positional[1:]
        if missing:
            state["violations"].append(f"{command or 'call'} without {' '.join(missing)}")
            out.append(f"fake ddcutil: refusing a call without {' '.join(missing)}")
            rc = 2
        elif command == "--version":
            out.extend(["ddcutil 3.0.2", "Copyright (C) 2015-2026 Sanford Rockowitz"])
            rc = 0
        elif command == "detect":
            rc = detect(state, out)
        elif command == "capabilities":
            rc = capabilities(state, options, out)
        elif command == "getvcp":
            rc = getvcp(state, options, args, out)
        elif command == "setvcp":
            rc = setvcp(state, options, args, out)
        else:
            state["violations"].append(f"subcommand {command!r}")
            out.append(f"Unrecognized command: {command}")
            rc = 1
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    # surrogateescape turns the lone surrogates in the capabilities output back into the raw,
    # non-UTF-8 bytes real ddcutil prints for 16-bit masks.
    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8", "surrogateescape"))
    sys.stdout.flush()
    log_call(path, {"id": call_id, "end": time.time(), "rc": rc})
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
