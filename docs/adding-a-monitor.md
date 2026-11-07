# Adding a monitor

osdless decides what a monitor can do from the monitor's own replies, so a new model often works
read-only without any change. What a new model needs is evidence: a report of what it answers, and,
for each write, one confirmed round trip on a real one. This page is for both sides: what to send,
and what a maintainer does with it.

## 1. Read it

These commands only read, and need no `--untested`:

```bash
osdless list
osdless status
osdless features
```

`features` prints the profile and every step that chose it. If the profile is `read-only`, the last
line of that block says why. For an ASUS gaming monitor on VCP table 2 it should be
`asus-v2-gaming`, with each write `ready` (verified on your model), `needs --untested <model>`, or
the reason it is not offered.

## 2. Write a report

```bash
osdless report --output .
```

It reads every code the monitor lists in its capabilities, except the codes osdless never sends, in
batches of 8, and writes nothing. It leaves `osdless-report-<model>-<date>.json` in the directory and
prints a summary. The serial number is replaced with `<redacted>`; check the file before you share
it anyway, since a capabilities string can contain other text the monitor chose to put there.

Then open a
[monitor report](https://github.com/ovflowd/osdless/issues/new?template=monitor_report.yml) and
attach the file. It helps a lot to add:

- the firmware version from the OSD's information page;
- what the OSD shows for a few settings `osdless status` decodes, such as GameVisual, Power Setting,
  KVM and the OLED care toggles, so the decoding can be checked against it;
- the connection: DisplayPort, USB-C or HDMI, directly or through a dock or MST hub.

## 3. From report to fixture (maintainers)

Every report is a test fixture. The fake ddcutil in `tests/fake_ddcutil.py` replays a monitor from
the `display`, `capabilities`, `registers` and `read_errors` of a report, so adding a model is:

1. Copy the report to `tests/fixtures/<MODEL>.json`.
2. Replace `"<redacted>"` in `display.serial` with a synthetic serial, `SYNTHETIC00NN`, the next
   number not yet used. Fill in `display.connector` (such as `card1-DP-1`) if it is empty. Drop the
   keys the fake does not use: `osdless`, `ddcutil`, `system`, `profile` and `features`.
3. Add `provenance`: where the data comes from, the date, the firmware, the ddcutil version and the
   connection, and what is synthetic.
4. Add `expect`: the profile osdless should choose and, for each write command, the status
   `features --json` should give (`verified`, `untested` or `unavailable`).
5. Run `uv run pytest`. `tests/test_profiles.py` runs `features` against every fixture and checks
   `expect`.

If the profile is wrong for the monitor, that is a bug in the profile rules, not in the fixture: fix
the rule, with a test that fails without the fix.

## 4. Verifying a write

A write moves from UNTESTED to VERIFIED for one model after one round trip on a real monitor of
that model: write, read back, and write the old value back. Do one feature at a time, at a time
when a dark screen or a moved USB hub does not hurt.

`<MODEL>` is your model as `osdless list` shows it, such as `PG32UCDM`:

```bash
osdless status                                  # note the current value
osdless oled saver off --untested <MODEL>       # the write
osdless status                                  # check it, and check that the OSD shows it too
osdless oled saver on --untested <MODEL>        # back to how it was
journalctl -t osdless --since today             # the journal lines of both writes
```

Each write prints `old -> new, confirmed` and logs a `sending` line and a `confirmed` line. Post the
commands, their output, the journal lines and what the OSD showed before and after, in the monitor
report. Then, in one pull request:

- add `("<MODEL>", "<feature>")` to `VERIFIED` in `osdless.py`;
- change that cell in the README's [support matrix](../README.md#supported-monitors) to VERIFIED
  (`tests/test_readme.py` checks that the two agree);
- change the fixture's `expect` to `verified`, and mention the round trip in its `provenance`.

`oled clean`, `kvm` and `input` are disruptive: the screen goes dark for minutes, the USB hub moves
to another computer, or the screen goes blank. For those, say in the report how you got back.

## 5. A new product line or another maker

osdless writes to ASUS gaming monitors on VCP table 2 only, because the same codes and bits mean
different things on other tables, other ASUS lines and other makers. Supporting writes on one of
them needs a new profile, and a new profile needs, for each setting it writes:

- what each code and bit means on that line, from the OSD: change one item in the OSD, run
  `osdless status` or `osdless get CODE`, change it back, and note which code or bit moved. This
  sends no writes from the computer;
- one verified round trip, as above.

Open a feature request with that evidence before writing the code.

## Things osdless will not do, even for a new model

- Read codes the monitor does not list in its capabilities. Some answer with another code's data, and
  `0xFE` only times out.
- Send the codes on `NEVER_SEND`, or write the ones on `NEVER_WRITE` (top of `osdless.py`).
- Write a raw value, brightness, a reset or firmware.
- Use the capabilities strings in this repository's fixtures in place of a failed live read.
