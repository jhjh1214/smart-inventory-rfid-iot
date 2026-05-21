import network
import time
from config import *

print("\n=== SMART INVENTORY SYSTEM BOOTING ===")

sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.connect(WIFI_SSID, WIFI_PASSWORD)

print("Connecting to WiFi...")
for i in range(20):
    if sta.isconnected():
        break
    time.sleep(1)
    print(".", end="")

if sta.isconnected():
    print("\nWiFi Connected!")
    print("IP:", sta.ifconfig()[0])
else:
    print("\nWiFi Failed!")
print("=====================================\n")