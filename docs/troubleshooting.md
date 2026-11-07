# Troubleshooting

- **"ddcutil detect found no DDC/CI display".** Check that `ddcutil detect` finds the monitor as your
  user, that DDC/CI is on in the OSD, and that the monitor shows this computer's input.
- **"ddcutil detect failed (exit N: …)".** ddcutil itself failed, and its own message follows. An
  "unrecognized option" means a ddcutil older than 2.1, which does not know
  `--enable-cross-instance-locks`.
- **"N displays match".** Add `--serial`, `--model`, `--connector` or `--bus`; `osdless list` shows
  them all, with their serial numbers.
- **"… is not in this monitor's capabilities".** The monitor does not list that code, bit or value,
  so osdless will not send it. `osdless features` shows what it lists.
- **"has not been verified on the …".** The write is untested on your model. Re-run with
  `--untested <your model>` if you accept that, then please send a [report](../README.md#reporting-a-monitor).
- **"only reads this monitor" or "read-only".** Either the profile is `read-only` (`osdless features`
  says why), or the bus maps to no DRM connector, as with the NVIDIA driver, MST hubs and some docks,
  or it is the separate `ddc` adapter of a DisplayPort connector.
- **"is not an OLED by ASUS's naming rule".** The `oled` commands are offered only on ROG and TUF
  Gaming OLEDs. If your model is an OLED whose name does not fit the rule, please send a
  [report](../README.md#reporting-a-monitor).
- **"the monitor's own input is HDMI … on a DisplayPort connector".** The monitor is connected
  through an adapter or a dock, and writes over HDMI are untested. `--untested <your model>` allows
  them.
- **"the monitor reports no serial number".** Choose it with `--connector` or `--bus` for writes.
- **Exit 4, "not confirmed".** The monitor did not show the new value. Run `osdless status` before
  trying again: for `kvm` and `input`, the switch may have happened after the read-back.
- **"reads 0xFE/0xFE (not available in the current mode)".** The setting is greyed out in the OSD in
  the current mode, for example by VRR or HDR. Change the mode first.
- **The capabilities read fails or times out.** Some monitors stop answering while the panel is
  dimmed or in standby. Wake the screen and try again.
- **"another osdless run holds …".** Another osdless command is running. It holds the lock for as
  long as it runs, which for `oled clean --wait` can be 10 minutes.
