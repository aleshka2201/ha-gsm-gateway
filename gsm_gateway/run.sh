#!/usr/bin/with-contenv bashio

bashio::log.info "GSM MQTT Gateway v1.4.0 starting..."

# Export all settings as env vars — gen_config.py reads them
export GW_SERIAL_PORT=$(bashio::config 'serial_port')
export GW_SERIAL_BAUD=$(bashio::config 'serial_baudrate')
export GW_SERIAL_WD=$(bashio::config 'serial_watchdog_timeout')
export GW_MQTT_HOST=$(bashio::config 'mqtt_host')
export GW_MQTT_PORT=$(bashio::config 'mqtt_port')
export GW_MQTT_USER=$(bashio::config 'mqtt_username')
export GW_MQTT_PASS=$(bashio::config 'mqtt_password')
export GW_MQTT_ID=$(bashio::config 'mqtt_client_id')
export GW_TOPIC_SMS_IN=$(bashio::config 'topic_sms_inbox')
export GW_TOPIC_SMS_OUT=$(bashio::config 'topic_sms_send')
export GW_TOPIC_CALL_IN=$(bashio::config 'topic_call_inbox')
export GW_TOPIC_CALL_OUT=$(bashio::config 'topic_call_dial')
export GW_TOPIC_STATUS=$(bashio::config 'topic_status')
export GW_STATUS_INTERVAL=$(bashio::config 'status_interval')
export GW_AT_TIMEOUT=$(bashio::config 'at_command_timeout')
export GW_LOG_LEVEL=$(bashio::config 'log_level')
# bashio returns JSON array for list type: ["+380..."]
export GW_TRUSTED=$(bashio::config 'trusted_numbers')

bashio::log.info "Serial: ${GW_SERIAL_PORT} | MQTT: ${GW_MQTT_HOST}:${GW_MQTT_PORT}"

# Generate config via Python (avoids YAML injection from bash)
if ! python3 /gen_config.py; then
    bashio::log.error "Config generation failed!"
    exit 1
fi

bashio::log.info "Config OK"

# Web UI — independent process, survives gateway crashes
nohup python3 /webui.py >/tmp/webui.log 2>&1 &
bashio::log.info "Web UI started (port 8099)"

# Gateway loop — auto-restart on crash
while true; do
    bashio::log.info "Starting gateway..."
    python3 /gateway.py /tmp/gw.yaml
    RC=$?
    bashio::log.warning "Gateway exited (rc=${RC}), restarting in 5s..."
    sleep 5
done
