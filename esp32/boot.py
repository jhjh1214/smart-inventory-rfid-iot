import network
import time
import config

print('\n=== SMART INVENTORY SYSTEM BOOTING ===')

sta = network.WLAN(network.STA_IF)
sta.active(True)

connected = False

for net in config.WIFI_NETWORKS:
    # Skip placeholder entries not yet filled in
    if net['ssid'].startswith('CAMPUS_') or net['broker'].startswith('CAMPUS_'):
        print('Skipping unconfigured entry:', net['ssid'])
        continue

    # Already on this network — just record broker and continue
    if sta.isconnected():
        try:
            if sta.config('essid') == net['ssid']:
                config.MQTT_BROKER = net['broker']
                print('Already connected:', net['ssid'], '|', sta.ifconfig()[0])
                print('Broker:', config.MQTT_BROKER)
                connected = True
                break
        except Exception:
            pass
        sta.disconnect()
        time.sleep(1)

    print('Trying WiFi:', net['ssid'], '...')
    sta.connect(net['ssid'], net['password'])

    for _ in range(20):
        if sta.isconnected():
            break
        time.sleep(0.5)

    if sta.isconnected():
        config.MQTT_BROKER = net['broker']
        print('WiFi OK:', net['ssid'], '|', sta.ifconfig()[0])
        print('Broker set to:', config.MQTT_BROKER)
        connected = True
        break
    else:
        print('Failed:', net['ssid'])
        sta.disconnect()
        time.sleep(1)

if not connected:
    print('ERROR: No WiFi network available — running offline')

print('=====================================\n')
