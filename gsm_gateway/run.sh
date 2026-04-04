#!/usr/bin/with-contenv bashio

bashio::log.info "Starting GSM MQTT Gateway v1.3.0..."

# Передаємо всі параметри як env змінні — Python сам будує конфіг
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
export GW_TRUSTED=$(bashio::config 'trusted_numbers')

bashio::log.info "Serial: ${GW_SERIAL_PORT}, MQTT: ${GW_MQTT_HOST}:${GW_MQTT_PORT}"

# Генеруємо конфіг через Python
python3 /gen_config.py
if [ $? -ne 0 ]; then
    bashio::log.error "Failed to generate config!"
    exit 1
fi

# Показуємо trusted номери для діагностики
python3 -c "
import yaml
with open('/tmp/gateway_config.yaml') as f:
    cfg = yaml.safe_load(f)
nums = cfg.get('gateway', {}).get('trusted_numbers', [])
print(f'[DIAG] Trusted numbers in config: {nums}')
"

bashio::log.info "Config OK"

# Web UI — незалежний процес
nohup python3 /webui.py > /tmp/webui.log 2>&1 &
bashio::log.info "Web UI started on port 8099"

# Gateway restart loop
while true; do
    bashio::log.info "Starting gateway..."
    python3 /gateway.py /tmp/gateway_config.yaml
    CODE=$?
    bashio::log.warning "Gateway exited (code ${CODE}), restart in 5s..."
    sleep 5
done
