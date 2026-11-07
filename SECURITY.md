# Security policy

## Reporting a vulnerability

Please report a vulnerability privately, through
[GitHub's private vulnerability reporting](https://github.com/ovflowd/osdless/security/advisories/new),
and not in a public issue. Include what you found, how to reproduce it and what it affects. You will
get an answer within a week.

If that page does not open, open an issue that only says you have a security problem to report, and
without any detail, so that a way to talk privately can be arranged.

## Scope

osdless is a wrapper around [ddcutil](https://www.ddcutil.com) that writes to monitor registers. In
scope are problems in this repository's code, for example a way to make osdless:

- send a code on its never-send list, or write one on its never-write list;
- write without a fresh read, outside the monitor's capabilities, or more than the one bit or value
  asked for;
- write to a monitor or bus other than the one chosen, or to a monitor its profile only reads;
- send `setvcp` from a read-only command;
- start pixel cleaning without its guards;
- write files outside its cache, state, lock and report paths, or put the serial number in a
  report.

Problems in ddcutil, the kernel's I2C or DRM drivers, the monitor's firmware or your desktop's power
manager belong to those projects.

## What osdless touches

See [Privacy](README.md#privacy) and the [safety model](README.md#safety-model) in the README: what
it reads, what it writes, and what it never sends.

## Supported versions

Only the latest commit on `main`.
