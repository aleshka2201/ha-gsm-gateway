# GSM MQTT Gateway — Home Assistant Addon

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Addon-blue?logo=home-assistant)](https://www.home-assistant.io/)
[![SIM800](https://img.shields.io/badge/Modem-SIM800-green)](https://simcom.ee/modules/sim800/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Аддон для **Home Assistant OS** який перетворює USB GSM модем (SIM800) на повноцінний двосторонній шлюз між мережею GSM і MQTT брокером.

Відправляй та отримуй SMS, реагуй на вхідні дзвінки, керуй розумним будинком через звичайний телефон — без інтернету і хмарних сервісів.

---

## Можливості

- **Вхідні SMS** — публікує в MQTT з текстом, номером відправника і часом
- **Вихідні SMS** — надсилає SMS за командою через MQTT
- **Вхідні дзвінки** — публікує CallerID в MQTT і автоматично скидає дзвінок
- **Вихідні дзвінки** — ініціює дзвінок за командою через MQTT
- **Довірені номери** — кожне повідомлення містить прапор `trusted: 1/0`
- **Статус модему** — рівень сигналу, оператор, реєстрація в мережі — публікується в MQTT регулярно
- **Web UI** — вбудований веб-інтерфейс з живим логом і статусом модему
- **Підтримка кирилиці** — SMS кодуються в UCS2, підтримка будь-яких мов
- **Автовідновлення** — watchdog на serial порт, автореконект до MQTT
- **Крос-платформний** — працює на amd64, aarch64, armv7, armhf (Raspberry Pi тощо)

---

## Підтримуване обладнання

| Модем | Інтерфейс | Статус |
|-------|-----------|--------|
| SIM800 USB Stick | USB → `/dev/ttyUSB0` | Протестовано |
| SIM800L + USB-UART | USB → `/dev/ttyUSB0` | Протестовано |
| SIM800C | USB → `/dev/ttyACM0` | Сумісний |
| Інші AT-модеми | USB serial | Може працювати |

---

## Встановлення

### Крок 1 — Додати репозиторій в Home Assistant

1. Перейди: **Налаштування → Додатки → Магазин додатків**
2. Натисни **⋮** (три крапки) у правому верхньому куті
3. Вибери **«Repositories»**
4. Вставте посилання:
   ```
   https://github.com/your-username/ha-gsm-gateway
   ```
5. Натисни **ADD** → закрий вікно → онови сторінку

### Крок 2 — Встановити аддон

1. У магазині знайди **«GSM MQTT Gateway»**
2. Натисни → **ВСТАНОВИТИ**
3. Зачекай збірки Docker образу (2–5 хвилин)

### Крок 3 — Налаштувати

Перейди на вкладку **Конфігурація** і заповни параметри:

| Параметр | Опис | Приклад |
|----------|------|---------|
| `serial_port` | Порт USB модему | `/dev/ttyUSB0` |
| `serial_baudrate` | Швидкість порту | `115200` |
| `serial_watchdog_timeout` | Таймаут watchdog (сек) | `60` |
| `mqtt_host` | IP або hostname MQTT брокера | `192.168.1.100` або `core-mosquitto` |
| `mqtt_port` | Порт MQTT | `1883` |
| `mqtt_username` | Логін MQTT | `user` |
| `mqtt_password` | Пароль MQTT | `password` |
| `mqtt_client_id` | Ідентифікатор клієнта | `gsm_gateway` |
| `trusted_numbers` | Список довірених номерів | `["+380501234567"]` |
| `status_interval` | Інтервал публікації статусу (сек) | `60` |
| `log_level` | Рівень логування | `INFO` |

> **Підказка:** якщо використовуєш вбудований Mosquitto аддон — в `mqtt_host` вкажи `core-mosquitto`

### Крок 4 — Запустити

1. Натисни **ЗБЕРЕГТИ**
2. Перейди на вкладку **Інфо** → натисни **ЗАПУСТИТИ**
3. Відкрий вкладку **Web UI** — там побачиш статус модему і логи

---

## Визначення порту USB модему

Якщо не знаєш який порт у твого модему:

**Налаштування → Система → Обладнання → Всі пристрої**

Знайди свій SIM800 — порт зазвичай:
- `/dev/ttyUSB0` — найпоширеніший варіант
- `/dev/ttyUSB1` — якщо є інші USB пристрої
- `/dev/ttyACM0` — деякі моделі

---

## Web UI

Після запуску аддону відкрий вкладку **«Відкрити веб-інтерфейс»** або перейди за адресою:

```
http://YOUR-HA-IP:8099
```

Інтерфейс показує:

| Поле | Опис |
|------|------|
| Статус | Online / Offline |
| Сигнал | Рівень у dBm |
| Оператор | Назва оператора |
| Реєстрація | Тип підключення до мережі |
| SIM карта | Готовність SIM |
| Журнал подій | Живий лог (оновлення кожні 10 сек) |

---

## MQTT топіки

### Вхідні повідомлення (Gateway → MQTT)

#### `gsm/sms/inbox` — вхідне SMS
```json
{
  "from": "+380501234567",
  "text": "Привіт, це тест!",
  "trusted": 1,
  "timestamp": "2024-01-15T13:45:00Z"
}
```

#### `gsm/call/inbox` — вхідний дзвінок (скидається автоматично)
```json
{
  "from": "+380501234567",
  "action": "hangup",
  "trusted": 1,
  "timestamp": "2024-01-15T13:45:00Z"
}
```

#### `gsm/status` — статус модему (кожні N секунд)
```json
{
  "timestamp": "2024-01-15T13:45:00Z",
  "online": true,
  "signal_rssi": 18,
  "signal_dbm": -77,
  "operator": "Kyivstar",
  "sim_ready": true,
  "registration": "registered_home"
}
```

> `trusted: 1` — номер є у списку `trusted_numbers`, `trusted: 0` — невідомий номер

### Вихідні команди (MQTT → Gateway)

#### `gsm/sms/send` — надіслати SMS
```json
{
  "to": "+380501234567",
  "text": "Повідомлення підтримує кирилицю!"
}
```

#### `gsm/call/dial` — зробити дзвінок
```json
{
  "to": "+380501234567"
}
```

---

## Сенсори в Home Assistant

Додай у `configuration.yaml` щоб мати сенсори рівня сигналу, оператора і статусу:

```yaml
mqtt:
  sensor:
    - name: "GSM Signal"
      state_topic: "gsm/status"
      value_template: "{{ value_json.signal_dbm }}"
      unit_of_measurement: "dBm"
      device_class: signal_strength
      icon: mdi:signal

    - name: "GSM Operator"
      state_topic: "gsm/status"
      value_template: "{{ value_json.operator }}"
      icon: mdi:sim

    - name: "GSM Registration"
      state_topic: "gsm/status"
      value_template: "{{ value_json.registration }}"
      icon: mdi:antenna

    - name: "GSM Online"
      state_topic: "gsm/status"
      value_template: "{{ value_json.online }}"
      icon: mdi:cellphone-wireless
```

---

## Приклади автоматизацій

### Вхідні SMS

#### Сповіщення в HA при будь-якому вхідному SMS

```yaml
alias: "GSM | Вхідне SMS → сповіщення"
trigger:
  - platform: mqtt
    topic: gsm/sms/inbox
action:
  - service: persistent_notification.create
    data:
      title: "📩 SMS від {{ trigger.payload_json.from }}"
      message: >
        {{ trigger.payload_json.text }}

        🕐 {{ trigger.payload_json.timestamp }}
        {% if trigger.payload_json.trusted == 1 %}
        ✅ Довірений номер
        {% else %}
        ⚠️ Невідомий номер
        {% endif %}
      notification_id: "gsm_sms_inbox"
mode: queued
max: 10
```

---

#### SMS команда керування світлом

Надішли SMS з текстом `світло on` або `світло off` щоб керувати світлом дистанційно.

```yaml
alias: "GSM | SMS команда → світло"
trigger:
  - platform: mqtt
    topic: gsm/sms/inbox
condition:
  - condition: template
    value_template: "{{ trigger.payload_json.trusted == 1 }}"
action:
  - choose:
      - conditions:
          - condition: template
            value_template: >
              {{ 'світло on' in trigger.payload_json.text | lower }}
        sequence:
          - service: light.turn_on
            target:
              entity_id: light.living_room
          - service: mqtt.publish
            data:
              topic: gsm/sms/send
              payload: >
                {"to": "{{ trigger.payload_json.from }}", "text": "✅ Світло увімкнено"}
      - conditions:
          - condition: template
            value_template: >
              {{ 'світло off' in trigger.payload_json.text | lower }}
        sequence:
          - service: light.turn_off
            target:
              entity_id: light.living_room
          - service: mqtt.publish
            data:
              topic: gsm/sms/send
              payload: >
                {"to": "{{ trigger.payload_json.from }}", "text": "❌ Світло вимкнено"}
mode: single
```

---

#### SMS запит статусу будинку

Надішли SMS з текстом `статус` — отримаєш у відповідь температуру і стан пристроїв.

```yaml
alias: "GSM | SMS 'статус' → відповідь"
trigger:
  - platform: mqtt
    topic: gsm/sms/inbox
condition:
  - condition: template
    value_template: >
      {{ trigger.payload_json.trusted == 1 and
         'статус' in trigger.payload_json.text | lower }}
action:
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: >
        {
          "to": "{{ trigger.payload_json.from }}",
          "text": "🏠 Статус:\nТемпература: {{ states('sensor.living_room_temperature') }}°C\nВологість: {{ states('sensor.living_room_humidity') }}%\nСвітло: {{ states('light.living_room') }}\nСигналізація: {{ states('alarm_control_panel.home') }}"
        }
mode: single
```

---

#### SMS з невідомого номера → сповіщення

```yaml
alias: "GSM | SMS невідомий → попередження"
trigger:
  - platform: mqtt
    topic: gsm/sms/inbox
condition:
  - condition: template
    value_template: "{{ trigger.payload_json.trusted == 0 }}"
action:
  - service: persistent_notification.create
    data:
      title: "⚠️ SMS з невідомого номера"
      message: >
        Від: {{ trigger.payload_json.from }}
        Текст: {{ trigger.payload_json.text }}
        Час: {{ trigger.payload_json.timestamp }}
      notification_id: "gsm_unknown_sms"
mode: queued
max: 5
```

---

### Вхідні дзвінки

#### Дзвінок від довіреного номера → відкрити ворота

Один дзвінок — ворота відкриваються. Без витрат на SMS, без інтернету.

```yaml
alias: "GSM | Дзвінок довіреного → ворота"
trigger:
  - platform: mqtt
    topic: gsm/call/inbox
condition:
  - condition: template
    value_template: "{{ trigger.payload_json.trusted == 1 }}"
action:
  - service: switch.turn_on
    target:
      entity_id: switch.gate        # замінити на свій entity
  - delay:
      seconds: 5
  - service: switch.turn_off
    target:
      entity_id: switch.gate
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: >
        {"to": "{{ trigger.payload_json.from }}", "text": "🚪 Ворота відкрито"}
mode: single
```

---

#### Дзвінок → сповіщення і push на телефон

```yaml
alias: "GSM | Вхідний дзвінок → сповіщення"
trigger:
  - platform: mqtt
    topic: gsm/call/inbox
action:
  - service: persistent_notification.create
    data:
      title: "📞 Вхідний дзвінок"
      message: >
        Дзвонить: {{ trigger.payload_json.from }}
        {% if trigger.payload_json.trusted == 1 %}✅ Довірений{% else %}⚠️ Невідомий{% endif %}
      notification_id: "gsm_call"
  - service: notify.mobile_app      # замінити на свій notify сервіс
    data:
      title: "📞 Дзвінок"
      message: "{{ trigger.payload_json.from }}"
mode: single
```

---

### Вихідні SMS

#### SMS при спрацюванні датчика руху вночі

```yaml
alias: "GSM | Рух вночі → SMS тривога"
trigger:
  - platform: state
    entity_id: binary_sensor.motion_hallway
    to: "on"
condition:
  - condition: time
    after: "23:00:00"
    before: "07:00:00"
action:
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: '{"to": "+380501234567", "text": "🚨 Рух у коридорі о {{ now().strftime(''%H:%M'') }}"}'
mode: single
```

---

#### SMS після перезапуску HA (відключення електрики)

```yaml
alias: "GSM | HA старт → SMS"
trigger:
  - platform: homeassistant
    event: start
action:
  - delay:
      seconds: 30
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: '{"to": "+380501234567", "text": "🔄 Система запустилась о {{ now().strftime(''%H:%M %d.%m.%Y'') }}"}'
mode: single
```

---

#### SMS при критичній температурі

```yaml
alias: "GSM | Критична температура → SMS"
trigger:
  - platform: numeric_state
    entity_id: sensor.living_room_temperature
    below: 5
action:
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: >
        {"to": "+380501234567", "text": "🥶 Температура впала до {{ states('sensor.living_room_temperature') }}°C!"}
mode: single
```

---

### Вихідні дзвінки

#### Дзвінок при спрацюванні сигналізації

```yaml
alias: "GSM | Тривога → дзвінок + SMS"
trigger:
  - platform: state
    entity_id: alarm_control_panel.home
    to: "triggered"
action:
  - service: mqtt.publish
    data:
      topic: gsm/call/dial
      payload: '{"to": "+380501234567"}'
  - delay:
      seconds: 30
  - service: mqtt.publish
    data:
      topic: gsm/sms/send
      payload: '{"to": "+380501234567", "text": "🚨 ТРИВОГА! Сигналізація спрацювала о {{ now().strftime(''%H:%M'') }}"}'
mode: single
```

---

### Моніторинг модему

#### Сповіщення якщо модем офлайн

```yaml
alias: "GSM | Модем офлайн → сповіщення"
trigger:
  - platform: mqtt
    topic: gsm/status
condition:
  - condition: template
    value_template: "{{ trigger.payload_json.online == false }}"
action:
  - service: persistent_notification.create
    data:
      title: "📡 GSM модем офлайн"
      message: >
        Модем втратив мережу.
        Сигнал: {{ trigger.payload_json.signal_rssi }} RSSI
        Реєстрація: {{ trigger.payload_json.registration }}
        Час: {{ trigger.payload_json.timestamp }}
      notification_id: "gsm_offline"
mode: single
```

---

## Структура репозиторію

```
ha-gsm-gateway/
├── README.md
├── repository.yaml
└── gsm_gateway/
    ├── config.yaml      — маніфест аддону, схема Options
    ├── Dockerfile       — Alpine Linux + Python + залежності
    ├── run.sh           — зчитує HA Options, генерує конфіг, запускає шлюз
    ├── gateway.py       — асинхронний GSM-MQTT шлюз
    └── webui.py         — веб-інтерфейс (порт 8099)
```

---

## Технічні деталі

- **Python 3.12** з повністю асинхронною архітектурою (`asyncio`)
- **aiomqtt** — нативно асинхронний MQTT клієнт
- **pyserial-asyncio** — асинхронна робота з serial портом
- **SMS кодування** — PDU режим, UCS2 (підтримка кирилиці та будь-яких мов)
- **Watchdog** — якщо serial порт завис на `watchdog_timeout` секунд — автоматичний перезапуск
- **MQTT reconnect** — автоматичне перепідключення при розриві зв'язку
- **Web UI** — незалежний процес, не залежить від стану gateway

---

## Ліцензія

MIT License — використовуй вільно для особистих і комерційних проектів.
