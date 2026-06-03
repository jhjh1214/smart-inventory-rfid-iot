from machine import Pin, SPI
from mfrc522 import MFRC522
from config import RFID_SCK, RFID_MOSI, RFID_MISO, RFID_CS, RFID_BLOCK, RFID_KEY


class RFIDReader:
    def __init__(self, spi=None, cs=None):
        """
        spi / cs can be passed in for multi-reader setups (shared SPI bus).
        When called with no arguments the reader creates its own SPI instance
        using the pins from config.py (backward-compatible single-reader mode).
        """
        if spi is None:
            spi = SPI(1, baudrate=1000000, polarity=0, phase=0,
                      sck=Pin(RFID_SCK), mosi=Pin(RFID_MOSI), miso=Pin(RFID_MISO))
            cs = Pin(RFID_CS, Pin.OUT)
        self.reader = MFRC522(spi, cs)

    # ── Read ──────────────────────────────────────────────────────────────────

    def read_tag(self):
        """Return (uid_str, item_id) or (None, None) if no tag present."""
        stat, _ = self.reader.request(self.reader.REQIDL)
        if stat != self.reader.OK:
            return None, None

        stat, raw_uid = self.reader.anticoll()
        if stat != self.reader.OK:
            return None, None

        uid_str = ''.join('%02X' % b for b in raw_uid)
        item_id = None

        if self.reader.select_tag(raw_uid) == self.reader.OK:
            if self.reader.auth(self.reader.AUTHENT1A, RFID_BLOCK,
                                RFID_KEY, raw_uid) == self.reader.OK:
                data = self.reader.read(RFID_BLOCK)
                self.reader.stop_crypto1()
                if data:
                    try:
                        item_id = bytes(data).decode('utf-8').strip('\x00').strip()
                        if not item_id:
                            item_id = None
                    except Exception:
                        item_id = None

        return uid_str, item_id

    # ── Write ─────────────────────────────────────────────────────────────────

    def write_item_id(self, item_id):
        """
        Detect a tag, check if it is blank, and optionally write item_id.

        item_id — string to write, or None to only detect without writing.

        Returns (uid_str, existing_id, wrote_ok):
          (None,    None,        False)  — no tag on reader
          (uid,     existing_id, False)  — tag already has data; did not overwrite
          (uid,     None,        False)  — blank tag but item_id=None; nothing written
          (uid,     None,        True)   — blank tag, write succeeded
        """
        stat, _ = self.reader.request(self.reader.REQIDL)
        if stat != self.reader.OK:
            return None, None, False

        stat, raw_uid = self.reader.anticoll()
        if stat != self.reader.OK:
            return None, None, False

        uid_str = ''.join('%02X' % b for b in raw_uid)

        if self.reader.select_tag(raw_uid) != self.reader.OK:
            self.reader.stop_crypto1()
            return uid_str, None, False

        if self.reader.auth(self.reader.AUTHENT1A, RFID_BLOCK,
                            RFID_KEY, raw_uid) != self.reader.OK:
            self.reader.stop_crypto1()
            return uid_str, None, False

        # Read existing content (single auth session covers both read and write)
        data = self.reader.read(RFID_BLOCK)
        existing_id = None
        if data:
            try:
                existing_id = bytes(data).decode('utf-8').strip('\x00').strip()
                if not existing_id:
                    existing_id = None
            except Exception:
                pass

        if existing_id:
            # Tag already has data — never overwrite
            self.reader.stop_crypto1()
            return uid_str, existing_id, False

        if not item_id:
            # Blank tag but no write target provided
            self.reader.stop_crypto1()
            return uid_str, None, False

        # Write item_id as 16-byte zero-padded list
        encoded = list(item_id.encode('utf-8')[:16])
        encoded += [0] * (16 - len(encoded))
        result = self.reader.write(RFID_BLOCK, encoded)
        self.reader.stop_crypto1()

        return uid_str, None, result == self.reader.OK
