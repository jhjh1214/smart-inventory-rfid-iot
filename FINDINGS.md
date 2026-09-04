# Bootstrap Findings

Environment and setup issues found standing this project up on a new machine, and what was
done about each. Product-level gaps (security, analytics, RBAC) live in `CLAUDE.md` §9 and are
deliberately untouched here.

Last bootstrap: 2026-09-04 — Windows 11, Python 3.12.10, Mosquitto 2.1.2.

## Resolved

| # | Finding | Resolution |
|---|---------|------------|
| 1 | `start.ps1` hardcoded `C:\Program Files (x86)\Mosquitto\mosquitto.exe`; the x64 installer uses `C:\Program Files\mosquitto`, so the launcher aborted with "Mosquitto missing" on a machine where the broker was installed. | `start.ps1` now probes both paths and then PATH, and names the winget install command if none is found. |
| 2 | `start.ps1` ran `python -m pip install` against whatever `python` was first on PATH, polluting machine-wide site-packages. | `start.ps1` now creates `.venv` if absent, installs into it, and launches the backend with it. |
| 3 | The winget package registers `mosquitto` as an **auto-starting Windows service** with an empty config. Mosquitto 2.x with no config binds `127.0.0.1` only, so LAN clients were refused — and because a service runs in session 0, `start.ps1` died at "Stopping existing Mosquitto" with *Access denied*. | Service stopped and set to `StartupType Manual`, so `start.ps1`'s broker owns port 1883 with this repo's `mosquitto.conf`. Documented as README step 2b. |
| 4 | No firewall rules for 1883/5000, and the Wi-Fi adapter was classified **Public**, so the README's "open the Private profile" instruction would not have helped. | Wi-Fi reclassified Private; two inbound allow rules added, scoped to Private. Verified reachable from a separate network stack. README step 8 now says to check `NetworkCategory` first. |
| 5 | `tools/esptoolenv/` was copied from another device — its `pyvenv.cfg` pointed at `C:\Users\yying\...\Python314`, which does not exist here, so no ESP32 tool would run. | Deleted and rebuilt locally on Python 3.12. Pins now live in `tools/requirements-esp32.txt`. `rshell` was dropped: its `pyreadline` dependency calls `collections.Callable`, removed in Python 3.10, and nothing in the project uses it. |
| 6 | `esp32/config.py` listed only ***REMOVED***, Tong@unifi and an iPhone hotspot, with broker IPs from the old laptop. This machine is on `Maxlol@unifi` at 192.168.0.129, so no board could reach it. | Entry added for the current network with broker `192.168.0.129`, and the stale `MQTT_BROKER` fallback updated to match. **Left uncommitted — see below.** |
| 7 | Ten tests failed; two more passed for the wrong reason. | All twelve fixed in the tests only, no product code changed. Suite is 389 passed, 0 failed. Detail in `CLAUDE.md` §7. |

## Open

- **`esp32/config.py` holds a real Wi-Fi password and is tracked by git.** The new entry is in
  the working tree but deliberately not committed. Committing it adds another live credential to
  history — the same gap `CLAUDE.md` §9 already flags for the three existing entries. Decide
  whether these belong in a gitignored `config_local.py` (already in `.gitignore`) before this
  file is committed again.
- **No ESP32 was attached during bootstrap** — only Bluetooth COM ports were present. Flashing,
  tag reads, and a scan originating from real hardware are therefore unverified. Everything up to
  and including "a LAN client publishes and the backend records it" was verified with a local
  MQTT client.
- **`start.ps1` still hard-fails if a broker it cannot stop is running** (elevated, or as a
  service). Documented in README step 2b rather than guarded in code, since the fix is to stop
  the service once.

## Corrected

An earlier version of this file suggested shipping a `.env.example`. That was wrong: nothing in
the codebase reads a `.env` file — there is no `python-dotenv` dependency and no `load_dotenv()`
call. The environment variables are read directly via `os.environ.get` at import time and are
documented in `CLAUDE.md` §3. Adding `.env` support would be a feature, not a setup fix.
