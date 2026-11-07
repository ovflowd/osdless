# Contributing

Thanks for helping. osdless is small on purpose, and it writes to monitors, so a change should be
small too: one change per pull request, and a behavior change comes with a test.

## Setup

```sh
git clone https://github.com/ovflowd/osdless.git
cd osdless
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests need no monitor and no ddcutil, and take about two minutes. They run osdless against a
fake ddcutil (`tests/fake_ddcutil.py`) that replays a monitor from `tests/fixtures/`, with fake
inhibitors, a fake `logger` and a fake DRM tree, and `PATH` holds only those fakes. The fake keeps
its own list of codes osdless must never send, so a mistake in osdless's lists cannot hide there too.

To check your change against your own monitor, read-only:

```sh
OSDLESS_REAL_MONITOR=1 uv run pytest tests/test_real_monitor.py
```

## Style

- Ruff formats and lints, with the settings in `pyproject.toml` (120 columns). CI checks both.
- A comment says why, not what. Do not restate the code.
- Standard library only. The single file has to run with nothing but Python and ddcutil.
- Type hints on every function, `test_*` functions excepted.
- Commit messages are an imperative sentence with a type, as in `fix: keep the cache directory
  private`, and the body says why.

## Changing what osdless writes

- Read the [safety model](README.md#safety-model) first. Every write goes through
  `Monitor.set_value`, and every check sits in front of it. Do not add a second path to `setvcp`.
- A change to a gate comes with a test that fails without it.
- Adding a code to `NEVER_SEND` or `NEVER_WRITE` is always welcome. Removing one needs evidence
  and a good reason in the pull request.
- No raw writes, no `--force`, and no new write without evidence from a real monitor. See
  [docs/adding-a-monitor.md](docs/adding-a-monitor.md).

## Reporting a monitor

Run `osdless report --output .` and attach the file to a
[monitor report](https://github.com/ovflowd/osdless/issues/new?template=monitor_report.yml). It
reads only, and leaves out the serial number. [docs/adding-a-monitor.md](docs/adding-a-monitor.md)
explains how a report becomes a fixture, and how a write becomes VERIFIED for a model.

Fixtures and docs never hold real serial numbers, host names or paths from your machine: use a
synthetic serial such as `SYNTHETIC0009`.

## AI assistants

If you used an AI assistant for a change, say so in the pull request. You remain responsible for
it, and for having read it.

## License of contributions

By contributing, you agree that your work is licensed under the [MIT license](LICENSE). This
project follows a [code of conduct](CODE_OF_CONDUCT.md).

## Reporting a problem

Questions and ideas are welcome as issues too. For a bug, include `osdless --version` and the
commit, the ddcutil version, the monitor and its firmware, the connection, and the command with its
`--json` output. `list`, `status` and `features` print your serial number: replace it before you
paste. For a security problem, see [SECURITY.md](SECURITY.md).
