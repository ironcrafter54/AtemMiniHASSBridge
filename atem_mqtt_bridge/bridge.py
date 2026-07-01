#!/usr/bin/env python3
"""
atem_mqtt_bridge.py

Bridges a Blackmagic ATEM Mini (native UDP protocol, port 9910) to
Home Assistant via MQTT autodiscovery. Exposes:

  select.atem_program   - choose which source is on Program (PGM)
  select.atem_preview   - choose which source is on Preview (PVW)
  button.atem_cut        - CUT transition
  button.atem_auto       - AUTO (fade/mix) transition

Requires:
  pip install PyATEMMax paho-mqtt

Env vars:
  ATEM_IP        - IP of the ATEM Mini (required)
  MQTT_HOST       - MQTT broker host (required)
  MQTT_PORT       - MQTT broker port (default 1883)
  MQTT_USER       - MQTT username (optional)
  MQTT_PASS       - MQTT password (optional)
  DEVICE_NAME     - friendly name used in entity IDs/discovery (default "atem_mini")
"""

import os
import time
import signal
import sys
import json
import logging

import PyATEMMax
import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("atem-bridge")

ATEM_IP = os.environ["ATEM_IP"]
MQTT_HOST = os.environ["MQTT_HOST"]
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASS = os.environ.get("MQTT_PASS")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "atem_mini")

BASE_TOPIC = f"atem/{DEVICE_NAME}"
AVAIL_TOPIC = f"{BASE_TOPIC}/availability"
DISCOVERY_PREFIX = "homeassistant"

# Map friendly labels -> PyATEMMax videoSources constant names.
# Adjust/add entries here if your input labeling differs.
SOURCE_MAP = {
    "Camera 1": "input1",
    "Camera 2": "input2",
    "Camera 3": "input3",
    "Camera 4": "input4",
    "Color Bars": "colorBars",
    "Color 1": "color1",
    "Color 2": "color2",
    "Media Player 1": "mediaPlayer1",
    "Black": "black",
}
SOURCE_NAMES = list(SOURCE_MAP.keys())

switcher = PyATEMMax.ATEMMax()
mqttc = mqtt.Client(client_id=f"atem-bridge-{DEVICE_NAME}")

_last_published = {"program": None, "preview": None}


def device_info():
    return {
        "identifiers": [f"atem_{DEVICE_NAME}"],
        "name": "ATEM Mini",
        "manufacturer": "Blackmagic Design",
        "model": "ATEM Mini",
    }


def publish_discovery():
    dev = device_info()

    select_common = {
        "device": dev,
        "availability_topic": AVAIL_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "options": SOURCE_NAMES,
    }

    mqttc.publish(
        f"{DISCOVERY_PREFIX}/select/{DEVICE_NAME}/program/config",
        json.dumps({
            **select_common,
            "name": "Program",
            "unique_id": f"{DEVICE_NAME}_program",
            "state_topic": f"{BASE_TOPIC}/program/state",
            "command_topic": f"{BASE_TOPIC}/program/set",
            "icon": "mdi:video-input-hdmi",
        }),
        retain=True,
    )

    mqttc.publish(
        f"{DISCOVERY_PREFIX}/select/{DEVICE_NAME}/preview/config",
        json.dumps({
            **select_common,
            "name": "Preview",
            "unique_id": f"{DEVICE_NAME}_preview",
            "state_topic": f"{BASE_TOPIC}/preview/state",
            "command_topic": f"{BASE_TOPIC}/preview/set",
            "icon": "mdi:eye-outline",
        }),
        retain=True,
    )

    for action, label, icon in [
        ("cut", "Cut", "mdi:content-cut"),
        ("auto", "Auto Transition", "mdi:transition"),
    ]:
        mqttc.publish(
            f"{DISCOVERY_PREFIX}/button/{DEVICE_NAME}/{action}/config",
            json.dumps({
                "device": dev,
                "availability_topic": AVAIL_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
                "name": label,
                "unique_id": f"{DEVICE_NAME}_{action}",
                "command_topic": f"{BASE_TOPIC}/{action}/press",
                "icon": icon,
            }),
            retain=True,
        )

    log.info("Published MQTT discovery configs")


def name_for_source(video_source_value):
    """Reverse-lookup a raw ATEM videoSource integer back to a friendly label."""
    for label, const_name in SOURCE_MAP.items():
        if getattr(switcher.atem.videoSources, const_name) == video_source_value:
            return label
    return f"unknown ({video_source_value})"


def poll_and_publish():
    if not switcher.connected:
        return

    pgm = switcher.programInput[0].videoSource
    pvw = switcher.previewInput[0].videoSource

    pgm_name = name_for_source(pgm)
    pvw_name = name_for_source(pvw)

    if pgm_name != _last_published["program"]:
        mqttc.publish(f"{BASE_TOPIC}/program/state", pgm_name, retain=True)
        _last_published["program"] = pgm_name
        log.info(f"Program -> {pgm_name}")

    if pvw_name != _last_published["preview"]:
        mqttc.publish(f"{BASE_TOPIC}/preview/state", pvw_name, retain=True)
        _last_published["preview"] = pvw_name
        log.info(f"Preview -> {pvw_name}")


def on_mqtt_connect(client, userdata, flags, rc):
    log.info(f"Connected to MQTT broker (rc={rc})")
    client.publish(AVAIL_TOPIC, "online" if switcher.connected else "offline", retain=True)
    client.subscribe(f"{BASE_TOPIC}/program/set")
    client.subscribe(f"{BASE_TOPIC}/preview/set")
    client.subscribe(f"{BASE_TOPIC}/cut/press")
    client.subscribe(f"{BASE_TOPIC}/auto/press")
    publish_discovery()


def on_mqtt_message(client, userdata, msg):
    if not switcher.connected:
        log.warning("Ignoring command, ATEM not connected")
        return

    payload = msg.payload.decode().strip()

    try:
        if msg.topic == f"{BASE_TOPIC}/program/set":
            const_name = SOURCE_MAP.get(payload)
            if const_name:
                switcher.setProgramInputVideoSource(0, getattr(switcher.atem.videoSources, const_name))
            else:
                log.warning(f"Unknown program source requested: {payload}")

        elif msg.topic == f"{BASE_TOPIC}/preview/set":
            const_name = SOURCE_MAP.get(payload)
            if const_name:
                switcher.setPreviewInputVideoSource(0, getattr(switcher.atem.videoSources, const_name))
            else:
                log.warning(f"Unknown preview source requested: {payload}")

        elif msg.topic == f"{BASE_TOPIC}/cut/press":
            switcher.execCutME(0)

        elif msg.topic == f"{BASE_TOPIC}/auto/press":
            switcher.execAutoME(0)

    except Exception:
        log.exception("Error handling MQTT command")


def on_atem_connect(params):
    log.info("ATEM connected")
    mqttc.publish(AVAIL_TOPIC, "online", retain=True)


def on_atem_disconnect(params):
    log.warning("ATEM disconnected")
    mqttc.publish(AVAIL_TOPIC, "offline", retain=True)


def main():
    if MQTT_USER:
        mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    mqttc.will_set(AVAIL_TOPIC, "offline", retain=True)
    mqttc.on_connect = on_mqtt_connect
    mqttc.on_message = on_mqtt_message
    mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    mqttc.loop_start()

    switcher.registerEvent(switcher.atem.events.connect, on_atem_connect)
    switcher.registerEvent(switcher.atem.events.disconnect, on_atem_disconnect)

    log.info(f"Connecting to ATEM at {ATEM_IP}...")
    switcher.connect(ATEM_IP)

    def shutdown(*_):
        log.info("Shutting down")
        mqttc.publish(AVAIL_TOPIC, "offline", retain=True)
        mqttc.loop_stop()
        switcher.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        poll_and_publish()
        time.sleep(0.5)


if __name__ == "__main__":
    main()
