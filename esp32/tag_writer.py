# tag_writer.py — RFID tag writing utility for demo/setup
#
# HOW TO RUN:
#   1. mpremote connect COM7
#   2. Press Ctrl+C  (stops main.py)
#   3. At >>> type:  import tag_writer

from machine import Pin, SPI
from mfrc522 import MFRC522
from config import RFID_SCK, RFID_MOSI, RFID_MISO, RFID_CS, RFID_BLOCK, RFID_KEY
import time

# Must match item IDs in backend database
DEMO_ITEMS = [
    ("item-001", "USB Cable Type-C"),
    ("item-002", "HDMI Cable"),
    ("item-003", "AA Batteries (pack)"),
    ("item-004", "Ethernet Cable 2m"),
    ("item-005", "Mouse Pad"),
    ("item-006", "RFID Reader Module"),
    ("item-007", "ESP32 Dev Board"),
    ("item-008", "Jumper Wires (set)"),
]


def _prepare(text):
    b = bytearray(text.encode('utf-8'))
    while len(b) < 16:
        b.append(0)
    return bytes(b[:16])


def _write_tag(rdr, item_id):
    print("Place tag on reader (remove it between writes)...")
    while True:
        stat, _ = rdr.request(rdr.REQIDL)
        if stat != rdr.OK:
            time.sleep(0.1)
            continue

        stat, raw_uid = rdr.anticoll()
        if stat != rdr.OK:
            time.sleep(0.1)
            continue

        uid_str = ''.join('%02X' % b for b in raw_uid)
        print("Tag detected:", uid_str)

        if rdr.select_tag(raw_uid) != rdr.OK:
            print("ERROR: select_tag failed")
            return False

        if rdr.auth(rdr.AUTHENT1A, RFID_BLOCK, RFID_KEY, raw_uid) != rdr.OK:
            print("ERROR: authentication failed (wrong key?)")
            rdr.stop_crypto1()
            return False

        result = rdr.write(RFID_BLOCK, _prepare(item_id))
        if result != rdr.OK:
            rdr.stop_crypto1()
            print("ERROR: write failed")
            return False

        # Read back to verify
        readback = rdr.read(RFID_BLOCK)
        rdr.stop_crypto1()

        if not readback:
            print("ERROR: could not verify write")
            return False

        written = bytes(readback).decode('utf-8').strip('\x00')
        if written != item_id:
            print("ERROR: verify mismatch —", written)
            return False

        print("SUCCESS: tag", uid_str, "->", item_id)
        return True


# ── Init reader ───────────────────────────────────────────────────────────
spi = SPI(1, baudrate=1000000, polarity=0, phase=0,
          sck=Pin(RFID_SCK), mosi=Pin(RFID_MOSI), miso=Pin(RFID_MISO))
cs  = Pin(RFID_CS, Pin.OUT)
rdr = MFRC522(spi, cs)
print("\n=== RFID Tag Writer ===")

# ── Main loop ─────────────────────────────────────────────────────────────
while True:
    print("\nDemo items:")
    for i, (item_id, name) in enumerate(DEMO_ITEMS):
        print("  {}. {} - {}".format(i + 1, item_id, name))
    print("  c. Custom item ID")
    print("  q. Quit")

    choice = input("\nSelect: ").strip().lower()

    if choice == 'q':
        print("Tag writer done.")
        break

    elif choice == 'c':
        item_id = input("Enter item ID: ").strip()
        if not item_id:
            print("No item ID entered.")
            continue
        _write_tag(rdr, item_id)

    elif choice.isdigit() and 1 <= int(choice) <= len(DEMO_ITEMS):
        item_id, name = DEMO_ITEMS[int(choice) - 1]
        print("Writing: {} - {}".format(item_id, name))
        if _write_tag(rdr, item_id):
            # Offer to write same ID to more tags (same product, multiple units)
            while True:
                again = input("Write same ID to another tag? (y/n): ").strip().lower()
                if again == 'y':
                    print("Remove tag, then place next tag...")
                    time.sleep(1)
                    _write_tag(rdr, item_id)
                else:
                    break
    else:
        print("Invalid choice.")
