"""Генерує /tmp/gw.yaml з env змінних виставлених run.sh."""
import json, os, sys, yaml

def env(key, default=""):
    return os.environ.get(key, str(default)).strip()

def env_int(key, default):
    try:    return int(env(key, default))
    except: return default

def parse_trusted():
    raw = env("GW_TRUSTED", "[]")
    if not raw or raw in ("null", "[]", ""):
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip().strip("'\"") for x in data if str(x).strip()]
        return [str(data).strip().strip("'\"")] if data else []
    except json.JSONDecodeError:
        s = raw.strip().strip("[]'\"")
        return [s] if s else []

trusted = parse_trusted()
print(f"[gen_config] trusted_numbers: {trusted}", flush=True)

cfg = {
    "serial":  {"port": env("GW_SERIAL_PORT", "/dev/ttyUSB0"),
                "baudrate": env_int("GW_SERIAL_BAUD", 115200),
                "watchdog_timeout": env_int("GW_SERIAL_WD", 60)},
    "mqtt":    {"host": env("GW_MQTT_HOST", "core-mosquitto"),
                "port": env_int("GW_MQTT_PORT", 1883),
                "username": env("GW_MQTT_USER"),
                "password": env("GW_MQTT_PASS"),
                "client_id": env("GW_MQTT_ID", "gsm_gateway"),
                "keepalive": 30, "reconnect_interval": 5},
    "topics":  {"sms_inbox":  env("GW_TOPIC_SMS_IN",  "gsm/sms/inbox"),
                "sms_send":   env("GW_TOPIC_SMS_OUT", "gsm/sms/send"),
                "call_inbox": env("GW_TOPIC_CALL_IN", "gsm/call/inbox"),
                "call_dial":  env("GW_TOPIC_CALL_OUT","gsm/call/dial"),
                "status":     env("GW_TOPIC_STATUS",  "gsm/status")},
    "gateway": {"at_command_timeout": env_int("GW_AT_TIMEOUT", 10),
                "status_interval":    env_int("GW_STATUS_INTERVAL", 60),
                "log_level":          env("GW_LOG_LEVEL", "INFO"),
                "trusted_numbers":    trusted},
}

with open("/tmp/gw.yaml", "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

print("[gen_config] /tmp/gw.yaml written OK", flush=True)
