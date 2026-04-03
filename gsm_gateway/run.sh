#!/usr/bin/with-contenv bashio

bashio::log.info "Starting GSM MQTT Gateway..."

# Read options from HA addon configuration
SERIAL_PORT=$(bashio::config 'serial_port')
SERIAL_BAUDRATE=$(bashio::config 'serial_baudrate')
SERIAL_WATCHDOG=$(bashio::config 'serial_watchdog_timeout')
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USER=$(bashio::config 'mqtt_username')
MQTT_PASS=$(bashio::config 'mqtt_password')
MQTT_CLIENT=$(bashio::config 'mqtt_client_id')
TOPIC_SMS_INBOX=$(bashio::config 'topic_sms_inbox')
TOPIC_SMS_SEND=$(bashio::config 'topic_sms_send')
TOPIC_CALL_INBOX=$(bashio::config 'topic_call_inbox')
TOPIC_CALL_DIAL=$(bashio::config 'topic_call_dial')
TOPIC_STATUS=$(bashio::config 'topic_status')
STATUS_INTERVAL=$(bashio::config 'status_interval')
AT_TIMEOUT=$(bashio::config 'at_command_timeout')
LOG_LEVEL=$(bashio::config 'log_level')

# Build trusted_numbers as proper YAML list
# bashio::config returns a JSON array like ["+380501234567","+380671234567"]
# We convert it to YAML list items using Python
TRUSTED_YAML=$(python3 - <<'PYEOF'
import json, sys, subprocess
result = subprocess.run(
    ["bashio", "config", "trusted_numbers"],
    capture_output=True, text=True
)
raw = result.stdout.strip()
try:
    numbers = json.loads(raw)
    if isinstance(numbers, list) and numbers:
        print("\n".join(f'  - "{n}"' for n in numbers))
    else:
        print("  []")
except Exception:
    print("  []")
PYEOF
)

# If list is non-empty write items, else write empty list
if [ "$TRUSTED_YAML" = "  []" ]; then
    TRUSTED_BLOCK="trusted_numbers: []"
else
    TRUSTED_BLOCK="trusted_numbers:
${TRUSTED_YAML}"
fi

# Write generated config to /tmp/gateway_config.yaml
cat > /tmp/gateway_config.yaml << EOF
serial:
  port: "${SERIAL_PORT}"
  baudrate: ${SERIAL_BAUDRATE}
  timeout: ${AT_TIMEOUT}
  watchdog_timeout: ${SERIAL_WATCHDOG}

mqtt:
  host: "${MQTT_HOST}"
  port: ${MQTT_PORT}
  username: "${MQTT_USER}"
  password: "${MQTT_PASS}"
  client_id: "${MQTT_CLIENT}"
  keepalive: 30
  reconnect_interval: 5

topics:
  sms_inbox: "${TOPIC_SMS_INBOX}"
  sms_send: "${TOPIC_SMS_SEND}"
  call_inbox: "${TOPIC_CALL_INBOX}"
  call_dial: "${TOPIC_CALL_DIAL}"
  status: "${TOPIC_STATUS}"

gateway:
  status_interval: ${STATUS_INTERVAL}
  at_command_timeout: ${AT_TIMEOUT}
  log_level: "${LOG_LEVEL}"
  ${TRUSTED_BLOCK}
EOF

bashio::log.info "Config generated. Serial port: ${SERIAL_PORT}, MQTT: ${MQTT_HOST}:${MQTT_PORT}"

# Start Web UI as independent background process
# Using nohup so it survives gateway restarts
nohup python3 /webui.py > /tmp/webui.log 2>&1 &
WEBUI_PID=$!
bashio::log.info "Web UI started on port 8099 (PID: ${WEBUI_PID})"

# Gateway restart loop — Web UI залишається живим незалежно
while true; do
    bashio::log.info "Starting gateway process..."
    python3 /gateway.py /tmp/gateway_config.yaml
    EXIT_CODE=$?
    bashio::log.warning "Gateway exited with code ${EXIT_CODE}, restarting in 5s..."
    sleep 5
done
