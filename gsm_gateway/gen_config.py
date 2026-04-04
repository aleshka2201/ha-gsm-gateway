"""
Генератор конфігу для GSM Gateway.
Читає env змінні виставлені run.sh, будує gateway_config.yaml.
Використовує yaml.dump щоб уникнути будь-яких проблем з форматуванням.
"""

import json
import os
import sys
import yaml


def get(key: str, default="") -> str:
    return os.environ.get(key, default).strip()


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key, str(default)))
    except ValueError:
        return default


def parse_trusted() -> list:
    raw = get("GW_TRUSTED", "[]").strip()
    if not raw:
        return []
    # bashio повертає JSON масив: ["+380...", "+380..."]
    # або порожній рядок / null
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            result = [str(n).strip().strip("'\"") for n in parsed if str(n).strip()]
            print(f"[gen_config] Parsed trusted numbers: {result}", flush=True)
            return result
        if parsed:
            return [str(parsed).strip().strip("'\"")] 
    except json.JSONDecodeError:
        # Може бути просто рядок без дужок
        s = raw.strip().strip("'\"[]")
        if s:
            return [s]
    return []


cfg = {
    "serial": {
        "port":             get("GW_SERIAL_PORT", "/dev/ttyUSB0"),
        "baudrate":         get_int("GW_SERIAL_BAUD", 115200),
        "timeout":          get_int("GW_AT_TIMEOUT", 10),
        "watchdog_timeout": get_int("GW_SERIAL_WD", 60),
    },
    "mqtt": {
        "host":              get("GW_MQTT_HOST", "core-mosquitto"),
        "port":              get_int("GW_MQTT_PORT", 1883),
        "username":          get("GW_MQTT_USER", ""),
        "password":          get("GW_MQTT_PASS", ""),
        "client_id":         get("GW_MQTT_ID", "gsm_gateway"),
        "keepalive":         30,
        "reconnect_interval": 5,
    },
    "topics": {
        "sms_inbox":  get("GW_TOPIC_SMS_IN",   "gsm/sms/inbox"),
        "sms_send":   get("GW_TOPIC_SMS_OUT",  "gsm/sms/send"),
        "call_inbox": get("GW_TOPIC_CALL_IN",  "gsm/call/inbox"),
        "call_dial":  get("GW_TOPIC_CALL_OUT", "gsm/call/dial"),
        "status":     get("GW_TOPIC_STATUS",   "gsm/status"),
    },
    "gateway": {
        "status_interval":    get_int("GW_STATUS_INTERVAL", 60),
        "at_command_timeout": get_int("GW_AT_TIMEOUT", 10),
        "log_level":          get("GW_LOG_LEVEL", "INFO"),
        "trusted_numbers":    parse_trusted(),
    },
}

out = "/tmp/gateway_config.yaml"
with open(out, "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

print(f"[gen_config] Config written to {out}", flush=True)
print(f"[gen_config] Trusted: {cfg['gateway']['trusted_numbers']}", flush=True)
