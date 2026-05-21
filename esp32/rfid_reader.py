from machine import Pin, SPI
from mfrc522 import MFRC522
from config import RFID_SCK, RFID_MOSI, RFID_MISO, RFID_CS, RFID_BLOCK, RFID_KEY


class RFIDReader:
    def __init__(self):
        spi = SPI(1, baudrate=1000000, polarity=0, phase=0,
                  sck=Pin(RFID_SCK), mosi=Pin(RFID_MOSI), miso=Pin(RFID_MISO))
        cs = Pin(RFID_CS, Pin.OUT)
        self.reader = MFRC522(spi, cs)

    def read_tag(self):
        """Return (uid_str, item_id) from tag block data, or (None, None)."""
        stat, _ = self.reader.request(self.reader.REQIDL)
        if stat != self.reader.OK:
            return None, None

        stat, raw_uid = self.reader.anticoll()
        if stat != self.reader.OK:
            return None, None

        uid_str = ''.join('%02X' % b for b in raw_uid)

        item_id = None
        if self.reader.select_tag(raw_uid) == self.reader.OK:
            if self.reader.auth(self.reader.AUTHENT1A, RFID_BLOCK, RFID_KEY, raw_uid) == self.reader.OK:
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