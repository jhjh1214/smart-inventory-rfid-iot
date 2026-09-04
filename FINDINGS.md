# Bootstrap Findings

Environment-level issues found while standing this project up on a new machine.
These are **findings, not fixes** — nothing here has been changed. Product-level
gaps live in `CLAUDE.md` §9; this file covers setup and tooling only.

Last bootstrap: 2026-09-04, Windows 11, Python 3.12.10.

| # | Finding | Impact | Suggested fix |
|---|---------|--------|---------------|
| 1 | `start.ps1` hardcodes `C:\Program Files (x86)\Mosquitto\mosquitto.exe`. The current Mosquitto installer (2.1.2, x64) installs to `C:\Program Files\mosquitto`. | `start.bat` aborts with "Mosquitto missing" on any machine with a 64-bit install, even though the broker is present. | Probe both paths and use whichever exists, instead of a single hardcoded `$MOSQUITTO`. |
| 2 | `start.ps1` runs `python -m pip install -r requirements-pc.txt` against whatever `python` is first on PATH — the system interpreter, not a venv. | Pollutes the machine-wide site-packages; a second Python project can break this one's pins. | Create and use `.venv` in the launcher, or document the venv step in the README. |
| 3 | `tools/esptoolenv/pyvenv.cfg` points at `C:\Users\yying\AppData\Local\Programs\Python\Python314\python.exe`, which does not exist here. Its site-packages hold `cp314` binaries. | The ESP32 toolchain (`esptool`, `ampy`, `mpremote`) cannot run; flashing and tag-writing are unavailable until rebuilt. Does not affect the backend. | Rebuild per machine, as `CLAUDE.md` §2 already notes: `python -m venv tools/esptoolenv && tools/esptoolenv/Scripts/pip install esptool mpremote adafruit-ampy`. |
| 4 | `esp32/config.py` sets `broker = '192.168.0.115'` for SSID `Tong@unifi`; this host's LAN address is `192.168.0.129`. | Boards join Wi-Fi but never reach the broker — scans silently never arrive. | Re-run `ipconfig` and update each `WIFI_NETWORKS[].broker` before uploading firmware. |
| 5 | No `.env` / `.env.example` exists. All four MQTT variables and `SECRET_KEY` fall back to in-code defaults. | Works for a LAN demo; `SECRET_KEY` is a committed constant and `debug=True` is on. Already logged as a product gap in `CLAUDE.md` §9. | Ship a `.env.example` documenting the five variables from `CLAUDE.md` §3. |

## Verified working

- Backend, MQTT broker, tag state machine, SSE, RBAC, and analytics all confirmed
  running end to end — see the bootstrap report for the exact checks.
- Test suite: **377 passed, 10 failed** — the same ten documented in `CLAUDE.md` §7.
  No new failures under numpy 2.5.2, scikit-learn 1.9.0, or pytest 9.1.1.
