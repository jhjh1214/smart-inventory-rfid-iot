"""Point every attached ESP32 at this laptop, on whatever network it is on now.

Run this before a demo, after reconnecting to a hotspot, or any time the boards
stop appearing in the dashboard. It is safe to run repeatedly — a board that is
already correct is left untouched.

    tools\\esptoolenv\\Scripts\\python.exe tools\\sync_boards.py

What it does, per attached board:

    1. Reads the board's existing config.py.
    2. Finds the entry for the Wi-Fi network this laptop is currently on.
       Adds it if missing, taking the password from the Windows profile store.
    3. Sets that entry's broker to this laptop's current IP.
    4. Moves that entry first, so boot.py stops blocking on absent networks.
    5. Writes it back, verifies, and resets the board.

DEVICE_ID and READERS are never modified — each board keeps its own identity and
reader layout. Originals are backed up under tools/board-backups/ before any
write. That directory holds Wi-Fi passwords in plaintext and is gitignored.

Why this exists: iOS re-randomises the Personal Hotspot subnet, so a broker IP
hardcoded on a board goes stale on almost every reconnect. There is nothing to
reserve on the phone side, so the boards have to be re-pointed instead.

Flags:
    --dry-run     show what would change, write nothing
    --port COM5   act on one port only (default: every USB-serial board found)
    --no-reset    leave boards at the REPL instead of resetting them
    --no-verify   skip the post-write MQTT heartbeat check
"""

import argparse
import os
import re
import subprocess
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit('pyserial missing. Run this with tools\\esptoolenv\\Scripts\\python.exe')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MPREMOTE = os.path.join(HERE, 'esptoolenv', 'Scripts', 'mpremote.exe')
BACKUPS = os.path.join(HERE, 'board-backups')

# USB-serial bridges used by common ESP32 dev boards
USB_BRIDGES = {0x10C4, 0x1A86, 0x0403, 0x303A}

MOSQUITTO_SUB = [
    r'C:\Program Files\mosquitto\mosquitto_sub.exe',
    r'C:\Program Files (x86)\mosquitto\mosquitto_sub.exe',
]

SSID_RE = re.compile(r"'ssid':\s*'([^']*)'")
BROKER_RE = re.compile(r"('broker':\s*)'[^']*'")
ENTRY_RE = re.compile(r'\{.*?\}', re.S)
BLOCK_RE = re.compile(r'WIFI_NETWORKS\s*=\s*\[(.*?)\n\]', re.S)


# ── Windows network state ─────────────────────────────────────────────────────

def _netsh(args):
    try:
        out = subprocess.run(['netsh'] + args, capture_output=True, timeout=20)
        return out.stdout.decode('utf-8', 'replace')
    except Exception:
        return ''


def current_ssid():
    """SSID of the Wi-Fi network this laptop is associated with, or None."""
    text = _netsh(['wlan', 'show', 'interfaces'])
    state = re.search(r'^\s*State\s*:\s*(.+)$', text, re.M)
    if not state or 'connected' not in state.group(1).strip().lower():
        return None
    # 'SSID' matches before 'BSSID' because BSSID lines start with B
    m = re.search(r'^\s*SSID\s*:\s*(.+)$', text, re.M)
    return m.group(1).strip() if m else None


def current_ip():
    """This laptop's IPv4 address on the wireless interface, or None."""
    text = _netsh(['interface', 'ip', 'show', 'addresses'])
    block = None
    for chunk in re.split(r'\n(?=Configuration for interface)', text):
        if 'Wi-Fi' in chunk or 'Wireless' in chunk:
            block = chunk
            break
    if block is None:
        block = text
    for ip in re.findall(r'IP Address:\s*(\d+\.\d+\.\d+\.\d+)', block):
        if not ip.startswith(('127.', '169.254.')):
            return ip
    return None


def saved_password(ssid):
    """Password for a saved Windows Wi-Fi profile, or None if not stored."""
    text = _netsh(['wlan', 'show', 'profile', 'name=%s' % ssid, 'key=clear'])
    m = re.search(r'^\s*Key Content\s*:\s*(.+)$', text, re.M)
    return m.group(1).strip() if m else None


# ── Board conversation ────────────────────────────────────────────────────────

def find_ports():
    found = []
    for p in list_ports.comports():
        if p.vid in USB_BRIDGES:
            found.append(p.device)
    return sorted(found)


def interrupt(port, seconds=9.0):
    """Reset the board and hold Ctrl-C so boot.py's Wi-Fi loop cannot block us.

    boot.py connects to Wi-Fi before the REPL is reachable, so a board on an
    absent network is unreachable to mpremote until interrupted mid-boot.
    """
    try:
        s = serial.Serial(port, 115200, timeout=0.5)
    except Exception as e:
        return False, 'cannot open %s (%s)' % (port, e)
    try:
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.15)
        s.setRTS(False)
        buf = b''
        end = time.time() + seconds
        while time.time() < end:
            s.write(b'\x03')
            time.sleep(0.04)
            buf += s.read(256)
        s.write(b'\r\n')
        time.sleep(0.4)
        buf += s.read(4096)
        return (b'>>>' in buf), 'no REPL prompt'
    finally:
        s.close()


def reset(port):
    try:
        s = serial.Serial(port, 115200, timeout=0.5)
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.15)
        s.setRTS(False)
        s.close()
        return True
    except Exception:
        return False


def mp(port, *args, retries=3):
    """Run mpremote against an already-interrupted board (no soft reset)."""
    last = ''
    for attempt in range(retries):
        r = subprocess.run([MPREMOTE, 'connect', port, 'resume'] + list(args),
                           capture_output=True, timeout=90)
        if r.returncode == 0:
            return True, r.stdout.decode('utf-8', 'replace')
        last = r.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or ['']
        last = last[0]
        time.sleep(1.0)
    return False, last


# ── Config rewriting ──────────────────────────────────────────────────────────

def ssid_of(entry):
    m = SSID_RE.search(entry)
    return m.group(1) if m else ''


def patch(src, ssid, ip, password):
    """Return (new_source, list_of_changes). Never touches DEVICE_ID/READERS."""
    changes = []
    m = BLOCK_RE.search(src)
    if not m:
        raise ValueError('WIFI_NETWORKS block not found')
    entries = ENTRY_RE.findall(m.group(1))

    known = [ssid_of(e) for e in entries]
    if ssid not in known:
        if not password:
            raise ValueError(
                "network '%s' is not on the board and Windows has no saved "
                "password for it" % ssid)
        entries.insert(0, (
            "{\n"
            "        'ssid':     '%s',\n"
            "        'password': '%s',\n"
            "        'broker':   '%s',\n"
            "    }" % (ssid, password, ip)))
        changes.append("added network '%s'" % ssid)
    else:
        for i, e in enumerate(entries):
            if ssid_of(e) == ssid:
                old = re.search(r"'broker':\s*'([^']*)'", e)
                if old and old.group(1) != ip:
                    entries[i] = BROKER_RE.sub(
                        lambda mo: mo.group(1) + "'" + ip + "'", e)
                    changes.append('broker %s -> %s' % (old.group(1), ip))

    if known[:1] != [ssid]:
        entries.sort(key=lambda e: 0 if ssid_of(e) == ssid else 1)
        changes.append("moved '%s' first" % ssid)

    block = ('WIFI_NETWORKS = [\n'
             + '\n'.join('    ' + e + ',' for e in entries) + '\n]')
    out = src[:m.start()] + block + src[m.end():]

    fb = re.search(r"MQTT_BROKER\s*=\s*'([^']*)'", out)
    if fb and fb.group(1) != ip:
        out = re.sub(r"(MQTT_BROKER\s*=\s*)'[^']*'",
                     lambda mo: mo.group(1) + "'" + ip + "'", out, count=1)
        changes.append('fallback %s -> %s' % (fb.group(1), ip))

    # identity must survive untouched
    for key in ('DEVICE_ID', 'READERS'):
        a = re.search(key + r'\s*=\s*(.*?)\n\n', src, re.S)
        b = re.search(key + r'\s*=\s*(.*?)\n\n', out, re.S)
        if a and b and a.group(1) != b.group(1):
            raise ValueError('%s would change — refusing' % key)

    return out, changes


# ── Per-board flow ────────────────────────────────────────────────────────────

def sync_board(port, ssid, ip, password, dry_run, do_reset):
    ok, why = interrupt(port)
    if not ok:
        return {'port': port, 'id': '?', 'roles': '?', 'result': 'unreachable (%s)' % why}

    os.makedirs(BACKUPS, exist_ok=True)
    tmp_in = os.path.join(BACKUPS, '_read_%s.py' % port)
    ok, err = mp(port, 'cp', ':config.py', tmp_in)
    if not ok:
        return {'port': port, 'id': '?', 'roles': '?', 'result': 'read failed (%s)' % err}

    with open(tmp_in, encoding='utf-8') as fh:
        src = fh.read()

    dev = re.search(r"DEVICE_ID\s*=\s*'([^']*)'", src)
    dev = dev.group(1) if dev else '?'
    roles = ','.join(re.findall(r"'role':\s*'([^']*)'", src)) or '?'

    try:
        out, changes = patch(src, ssid, ip, password)
    except ValueError as e:
        return {'port': port, 'id': dev, 'roles': roles, 'result': 'skipped: %s' % e}

    if not changes:
        os.remove(tmp_in)
        if do_reset and not dry_run:
            reset(port)
        return {'port': port, 'id': dev, 'roles': roles, 'result': 'already correct'}

    if dry_run:
        os.remove(tmp_in)
        return {'port': port, 'id': dev, 'roles': roles,
                'result': 'would change: ' + '; '.join(changes)}

    stamp = time.strftime('%Y%m%d-%H%M%S')
    os.replace(tmp_in, os.path.join(BACKUPS, '%s-%s-config.py' % (dev, stamp)))

    tmp_out = os.path.join(BACKUPS, '_write_%s.py' % port)
    with open(tmp_out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(out)
    ok, err = mp(port, 'cp', tmp_out, ':config.py')
    os.remove(tmp_out)
    if not ok:
        return {'port': port, 'id': dev, 'roles': roles, 'result': 'WRITE FAILED (%s)' % err}

    # Verify by reading the file back, not by importing it: boot.py already ran
    # `import config`, so sys.modules on the board would hand back the module as
    # it was *before* the write and the check would be meaningless.
    tmp_check = os.path.join(BACKUPS, '_check_%s.py' % port)
    ok, err = mp(port, 'cp', ':config.py', tmp_check)
    good = False
    if ok and os.path.exists(tmp_check):
        with open(tmp_check, encoding='utf-8') as fh:
            good = fh.read().replace('\r\n', '\n') == out.replace('\r\n', '\n')
        os.remove(tmp_check)
    if not good:
        return {'port': port, 'id': dev, 'roles': roles,
                'result': 'written but VERIFY FAILED — restore from tools/board-backups'}

    if do_reset:
        reset(port)
    return {'port': port, 'id': dev, 'roles': roles, 'result': '; '.join(changes)}


def heartbeat_check(seconds=35):
    exe = next((p for p in MOSQUITTO_SUB if os.path.exists(p)), None)
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '-h', '127.0.0.1', '-p', '1883',
                            '-t', 'inventory/status', '-W', str(seconds)],
                           capture_output=True, timeout=seconds + 15)
        text = r.stdout.decode('utf-8', 'replace')
    except Exception:
        return None
    return sorted(set(re.findall(r'"device_id":\s*"([^"]+)"', text)))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--port')
    ap.add_argument('--no-reset', action='store_true')
    ap.add_argument('--no-verify', action='store_true')
    args = ap.parse_args()

    ssid = current_ssid()
    ip = current_ip()
    if not ssid:
        sys.exit('Not associated with a Wi-Fi network. Connect to the hotspot first.')
    if not ip:
        sys.exit('No IPv4 address on the wireless interface.')

    print('Network : %s' % ssid)
    print('Laptop  : %s' % ip)

    password = saved_password(ssid)

    ports = [args.port] if args.port else find_ports()
    if not ports:
        sys.exit('No USB-serial boards found. Check the cable, the hub, and that '
                 'the CP210x driver is installed.')
    print('Boards  : %s' % ', '.join(ports))
    if args.dry_run:
        print('Mode    : dry run, nothing will be written')
    print()

    rows = []
    for port in ports:
        print('  %s ...' % port, flush=True)
        rows.append(sync_board(port, ssid, ip, password,
                               args.dry_run, not args.no_reset))

    print()
    print('%-6s %-10s %-32s %s' % ('PORT', 'DEVICE', 'ROLES', 'RESULT'))
    print('-' * 96)
    for r in rows:
        print('%-6s %-10s %-32s %s' % (r['port'], r['id'], r['roles'], r['result']))

    failed = [r for r in rows if 'FAIL' in r['result'] or 'unreachable' in r['result']]

    if not args.dry_run and not args.no_verify and not args.no_reset:
        print()
        print('Waiting for heartbeats (boards publish every 30s) ...')
        seen = heartbeat_check()
        if seen is None:
            print('  mosquitto_sub not found — skipped. Is the broker running?')
        elif seen:
            print('  online: %s' % ', '.join(seen))
            expected = {r['id'] for r in rows if r['id'] != '?'}
            missing = expected - set(seen)
            if missing:
                print('  NOT reporting: %s' % ', '.join(sorted(missing)))
                print('  Check the broker is running and port 1883 is allowed inbound.')
        else:
            print('  no heartbeats. Check the broker is running and 1883 is allowed inbound.')

    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
