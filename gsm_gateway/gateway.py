"""
GSM-MQTT Gateway for SIM800 USB stick
Home Assistant Addon version
"""

import asyncio
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import aiomqtt
import serial_asyncio
import yaml

LOG_FILE = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"


# ─────────────────────────────────────────────
# Config loader
# ─────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────
# Logging — console + rotating file
# ─────────────────────────────────────────────

def setup_logging(level: str):
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]

    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        handlers.append(fh)
    except Exception:
        pass

    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt, handlers=handlers)


logger = logging.getLogger("gsm_gateway")


# ─────────────────────────────────────────────
# SMS PDU helpers
# ─────────────────────────────────────────────

def encode_ucs2(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def decode_ucs2(hex_str: str) -> str:
    try:
        return bytes.fromhex(hex_str).decode("utf-16-be")
    except Exception:
        return hex_str


def build_pdu(phone: str, text: str) -> tuple[str, int]:
    smsc = "00"
    pdu_type = "11"
    mr = "00"
    phone_clean = phone.lstrip("+")
    type_of_address = "91" if phone.startswith("+") else "81"
    if len(phone_clean) % 2 != 0:
        phone_clean += "F"
    phone_encoded = "".join(
        phone_clean[i + 1] + phone_clean[i] for i in range(0, len(phone_clean), 2)
    )
    phone_len = hex(len(phone.lstrip("+")))[2:].upper().zfill(2)
    pid = "00"
    dcs = "08"
    vp = "AA"
    text_encoded = encode_ucs2(text)
    udl = hex(len(text) * 2)[2:].upper().zfill(2)
    pdu = smsc + pdu_type + mr + phone_len + type_of_address + phone_encoded + pid + dcs + vp + udl + text_encoded
    pdu_len = len(pdu) // 2 - 1
    return pdu, pdu_len


# ─────────────────────────────────────────────
# Trusted number helpers
# ─────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    return re.sub(r"[\s\-\(\)]", "", phone)


def is_trusted(phone: str, trusted_list: list) -> bool:
    norm = normalize_phone(phone)
    for t in trusted_list:
        t_norm = normalize_phone(str(t))
        if norm == t_norm:
            return True
        if norm.lstrip("+") == t_norm.lstrip("+"):
            return True
    return False


# ─────────────────────────────────────────────
# Serial / AT layer
# ─────────────────────────────────────────────

class ATSerial:
    """
    Queue-based serial reader.

    Єдиний внутрішній reader-loop (_pump) безперервно читає рядки з порту
    і роздає їх:
      - у _at_queue  — якщо зараз виконується AT команда (is_at_busy=True)
      - у _urc_queue — всі інші рядки (URC, unsolicited)

    Це повністю усуває race condition між send_at() і _serial_reader().
    """

    def __init__(self, port: str, baudrate: int, at_timeout: float):
        self.port = port
        self.baudrate = baudrate
        self.at_timeout = at_timeout
        self._reader = None
        self._writer = None
        self._write_lock = asyncio.Lock()   # лок тільки на запис
        self._at_lock = asyncio.Lock()      # лок на час AT команди
        self._at_queue: asyncio.Queue = asyncio.Queue()
        self._urc_queue: asyncio.Queue = asyncio.Queue()
        self._is_at_busy = False
        self._connected = False
        self._last_activity = time.monotonic()
        self._pump_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_activity(self) -> float:
        return self._last_activity

    async def connect(self):
        logger.info(f"Connecting to serial {self.port} @ {self.baudrate}")
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )
        self._connected = True
        self._last_activity = time.monotonic()
        # Запускаємо єдиний pump loop
        self._pump_task = asyncio.create_task(self._pump(), name="serial_pump")
        logger.info("Serial connected")

    async def disconnect(self):
        self._connected = False
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _pump(self):
        """
        Єдиний reader coroutine — читає рядки і розподіляє по чергах.
        Ніхто більше не читає з self._reader напряму.
        """
        while self._connected:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=2.0)
                decoded = raw.decode(errors="replace").strip()
                if not decoded:
                    continue
                self._last_activity = time.monotonic()
                logger.debug(f"<< {decoded!r}")
                # Якщо зараз виконується AT команда — рядок іде в AT чергу
                if self._is_at_busy:
                    await self._at_queue.put(decoded)
                else:
                    await self._urc_queue.put(decoded)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._connected:
                    logger.error(f"Serial pump error: {e}")
                await asyncio.sleep(0.5)

    async def send_at(self, cmd: str, wait_for: str = "OK", timeout: float | None = None) -> str:
        """
        Надіслати AT команду і дочекатись відповіді.
        Під час виконання всі рядки з serial йдуть у _at_queue.
        """
        if not self._connected:
            raise ConnectionError("Serial not connected")
        timeout = timeout or self.at_timeout

        async with self._at_lock:
            # Очищаємо стару AT чергу перед новою командою
            while not self._at_queue.empty():
                self._at_queue.get_nowait()

            self._is_at_busy = True
            try:
                async with self._write_lock:
                    self._writer.write(f"{cmd}\r\n".encode())
                    await self._writer.drain()
                self._last_activity = time.monotonic()

                lines = []
                async with asyncio.timeout(timeout):
                    while True:
                        decoded = await self._at_queue.get()
                        if decoded:
                            lines.append(decoded)
                        if decoded in (wait_for, "ERROR", "NO CARRIER", "BUSY"):
                            break
                        if decoded.startswith("+CMS ERROR") or decoded.startswith("+CME ERROR"):
                            break
                self._last_activity = time.monotonic()
                return "\n".join(lines)

            except asyncio.TimeoutError:
                logger.warning(f"AT timeout: {cmd!r}")
                raise
            finally:
                self._is_at_busy = False

    async def read_urc(self, timeout: float = 1.0) -> str | None:
        """
        Отримати наступний URC рядок (unsolicited).
        Викликається з _serial_reader — без прямого читання з порту.
        """
        if not self._connected:
            return None
        try:
            return await asyncio.wait_for(self._urc_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"read_urc error: {e}")
            return None

    async def send_pdu_data(self, pdu: str, timeout: float = 30.0) -> str:
        """
        Надіслати PDU дані після промпту '>' для SMS.
        Використовує той самий AT lock і чергу.
        """
        if not self._connected:
            raise ConnectionError("Serial not connected")

        async with self._at_lock:
            while not self._at_queue.empty():
                self._at_queue.get_nowait()

            self._is_at_busy = True
            try:
                async with self._write_lock:
                    self._writer.write((pdu + "\x1A").encode())
                    await self._writer.drain()

                lines = []
                async with asyncio.timeout(timeout):
                    while True:
                        decoded = await self._at_queue.get()
                        if decoded:
                            lines.append(decoded)
                        if decoded in ("OK", "ERROR") or decoded.startswith("+CMS ERROR") or decoded.startswith("+CMGS"):
                            break
                return "\n".join(lines)
            except asyncio.TimeoutError:
                logger.error("PDU send timeout")
                raise
            finally:
                self._is_at_busy = False


# ─────────────────────────────────────────────
# Modem manager
# ─────────────────────────────────────────────

class ModemManager:
    def __init__(self, at: ATSerial, cfg: dict):
        self.at = at
        self.cfg = cfg

    async def init_modem(self):
        cmds = [
            ("AT", "OK"),
            ("ATE0", "OK"),
            ("AT+CMGF=0", "OK"),
            ("AT+CNMI=2,2,0,0,0", "OK"),
            ("AT+CLIP=1", "OK"),
            ("AT+CLTS=1", "OK"),
            ("AT+CSCS=\"UCS2\"", "OK"),
        ]
        for cmd, expect in cmds:
            try:
                await self.at.send_at(cmd, wait_for=expect)
            except Exception as e:
                logger.warning(f"Init {cmd!r} failed: {e}")
        logger.info("Modem initialized")

    async def get_status(self) -> dict:
        status = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "online": False,
            "signal_rssi": None,
            "signal_dbm": None,
            "operator": None,
            "sim_ready": False,
            "registration": None,
        }
        try:
            resp = await self.at.send_at("AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),(\d+)", resp)
            if m:
                rssi = int(m.group(1))
                status["signal_rssi"] = rssi
                if rssi != 99:
                    status["signal_dbm"] = -113 + rssi * 2
                    status["online"] = True

            resp = await self.at.send_at("AT+CPIN?")
            if "READY" in resp:
                status["sim_ready"] = True

            resp = await self.at.send_at("AT+CREG?")
            m = re.search(r"\+CREG:\s*\d+,(\d+)", resp)
            if m:
                reg_map = {"0": "not_registered", "1": "registered_home",
                           "2": "searching", "3": "denied", "5": "registered_roaming"}
                status["registration"] = reg_map.get(m.group(1), m.group(1))

            resp = await self.at.send_at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', resp)
            if m:
                try:
                    status["operator"] = decode_ucs2(m.group(1))
                except Exception:
                    status["operator"] = m.group(1)
        except Exception as e:
            logger.warning(f"Status error: {e}")

        # Write to status file for Web UI
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False)
        except Exception:
            pass

        return status

    async def send_sms(self, phone: str, text: str) -> bool:
        try:
            pdu, pdu_len = build_pdu(phone, text)
            # Крок 1: ініціювати передачу — чекаємо промпт '>'
            resp = await self.at.send_at(f"AT+CMGS={pdu_len}", wait_for=">", timeout=10)
            if ">" not in resp:
                logger.error(f"CMGS prompt missing: {resp!r}")
                return False
            # Крок 2: надіслати PDU через окремий метод з власним локом
            full = await self.at.send_pdu_data(pdu, timeout=30)
            if "OK" in full or "+CMGS" in full:
                logger.info(f"SMS sent to {phone}")
                return True
            logger.error(f"SMS failed: {full!r}")
            return False
        except Exception as e:
            logger.error(f"send_sms error: {e}")
            return False

    async def hangup(self):
        try:
            await self.at.send_at("ATH")
        except Exception as e:
            logger.warning(f"Hangup error: {e}")

    async def dial(self, phone: str) -> bool:
        try:
            resp = await self.at.send_at(f"ATD{phone};", wait_for="OK", timeout=15)
            return "OK" in resp
        except Exception as e:
            logger.error(f"Dial error: {e}")
            return False


# ─────────────────────────────────────────────
# URC parser
# ─────────────────────────────────────────────

class URCParser:
    def parse(self, line: str) -> dict | None:
        if line.startswith("+CMT:"):
            return {"type": "sms_header", "raw": line}
        m = re.match(r'\+CLIP:\s*"([^"]*)"', line)
        if m:
            return {"type": "call", "caller": _decode_safe(m.group(1))}
        if line == "RING":
            return {"type": "ring"}
        if line in ("NO CARRIER", "BUSY", "NO ANSWER"):
            return {"type": "call_ended", "reason": line}
        return None


def _decode_safe(s: str) -> str:
    try:
        return decode_ucs2(s)
    except Exception:
        return s


# ─────────────────────────────────────────────
# Main Gateway
# ─────────────────────────────────────────────

class GSMMQTTGateway:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.topics = cfg["topics"]
        self.serial_cfg = cfg["serial"]
        self.mqtt_cfg = cfg["mqtt"]
        self.gw_cfg = cfg["gateway"]

        self.at = ATSerial(
            port=self.serial_cfg["port"],
            baudrate=self.serial_cfg["baudrate"],
            at_timeout=self.gw_cfg["at_command_timeout"],
        )
        self.modem = ModemManager(self.at, cfg)
        self.urc = URCParser()

        self._mqtt_client = None
        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._trusted: list = []
        raw_trusted = self.gw_cfg.get("trusted_numbers", [])
        # Guard: може прийти None, int, або список
        if isinstance(raw_trusted, list):
            self._trusted = [normalize_phone(str(n)) for n in raw_trusted]
        elif raw_trusted:
            self._trusted = [normalize_phone(str(raw_trusted))]

    # ── Serial ────────────────────────────────

    async def _connect_serial(self):
        while self._running:
            try:
                await self.at.connect()
                await self.modem.init_modem()
                return
            except Exception as e:
                logger.error(f"Serial connect failed: {e}, retry in 5s")
                await asyncio.sleep(5)

    async def _serial_watchdog(self):
        timeout = self.serial_cfg["watchdog_timeout"]
        while self._running:
            await asyncio.sleep(10)
            if self.at.connected:
                idle = time.monotonic() - self.at.last_activity
                if idle > timeout:
                    logger.warning(f"Watchdog: {idle:.0f}s idle, reconnecting")
                    await self.at.disconnect()
                    await self._connect_serial()

    async def _serial_reader(self):
        sms_header = None
        while self._running:
            if not self.at.connected:
                await asyncio.sleep(1)
                continue
            try:
                # Читаємо з URC черги — не напряму з serial порту
                line = await self.at.read_urc(timeout=1.0)
                if not line:
                    continue

                # Двострочний SMS: заголовок потім PDU
                if sms_header is not None:
                    await self._handle_sms_pdu(sms_header, line)
                    sms_header = None
                    continue

                event = self.urc.parse(line)
                if event is None:
                    continue

                if event["type"] == "sms_header":
                    sms_header = event["raw"]
                elif event["type"] == "call":
                    caller = event["caller"]
                    await self._publish_call(caller)
                    await asyncio.sleep(0.5)
                    await self.modem.hangup()
                elif event["type"] == "ring":
                    logger.debug("RING — waiting for CLIP")

            except ConnectionError:
                logger.error("Serial lost, reconnecting")
                await asyncio.sleep(2)
                await self._connect_serial()
            except Exception as e:
                logger.error(f"Reader error: {e}")
                await asyncio.sleep(1)

    async def _handle_sms_pdu(self, header: str, pdu_line: str):
        try:
            phone, text = self._decode_incoming_pdu(pdu_line)
            trusted_flag = 1 if is_trusted(phone, self._trusted) else 0
            logger.info(f"SMS from {phone} (trusted={trusted_flag}): {text[:50]}")
            payload = json.dumps({
                "from": phone, "text": text,
                "trusted": trusted_flag,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }, ensure_ascii=False)
            await self._mqtt_publish(self.topics["sms_inbox"], payload)
        except Exception as e:
            logger.error(f"PDU error: {e}, raw={pdu_line!r}")

    def _decode_incoming_pdu(self, pdu: str) -> tuple[str, str]:
        idx = 0

        def read(n):
            nonlocal idx
            val = pdu[idx:idx + n]
            idx += n
            return val

        smsc_len = int(read(2), 16)
        read(smsc_len * 2)
        pdu_type = int(read(2), 16)
        oa_len = int(read(2), 16)
        oa_type = int(read(2), 16)
        oa_bytes = (oa_len + 1) // 2 * 2
        oa_raw = read(oa_bytes)

        if oa_type in (0x91, 0x81):
            phone = ""
            for i in range(0, len(oa_raw) - 1, 2):
                phone += oa_raw[i + 1]
                if oa_raw[i] != "F":
                    phone += oa_raw[i]
            if oa_type == 0x91:
                phone = "+" + phone
        else:
            phone = oa_raw

        read(2)  # pid
        dcs_raw = int(read(2), 16)
        vp_format = (pdu_type >> 3) & 0x03
        if vp_format == 0x02:
            read(2)
        elif vp_format in (0x01, 0x03):
            read(14)
        read(14)  # SCTS
        udl = int(read(2), 16)

        if dcs_raw & 0x08:
            text = decode_ucs2(pdu[idx:])
        elif (dcs_raw >> 2) & 0x03 == 0x01:
            text = bytes.fromhex(pdu[idx:]).decode("latin-1", errors="replace")
        else:
            text = self._decode_gsm7(pdu[idx:], udl)
        return phone, text

    @staticmethod
    def _decode_gsm7(hex_str: str, num_chars: int) -> str:
        gsm7 = (
            "@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
            "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u03b8\u039e\x1b\u00c6\u00e6\u00df\u00c9"
            " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
            "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
            "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0"
        )
        data = bytes.fromhex(hex_str)
        bits = int.from_bytes(data, "little")
        return "".join(
            gsm7[(bits >> (i * 7)) & 0x7F] if (bits >> (i * 7)) & 0x7F < len(gsm7) else "?"
            for i in range(num_chars)
        )

    # ── MQTT ──────────────────────────────────

    async def _mqtt_loop(self):
        interval = self.mqtt_cfg.get("reconnect_interval", 5)
        while self._running:
            try:
                logger.info(f"MQTT connecting to {self.mqtt_cfg['host']}:{self.mqtt_cfg['port']}")
                async with aiomqtt.Client(
                    hostname=self.mqtt_cfg["host"],
                    port=self.mqtt_cfg["port"],
                    username=self.mqtt_cfg["username"] or None,
                    password=self.mqtt_cfg["password"] or None,
                    identifier=self.mqtt_cfg["client_id"],
                    keepalive=self.mqtt_cfg["keepalive"],
                ) as client:
                    self._mqtt_client = client
                    logger.info("MQTT connected")
                    await client.subscribe(self.topics["sms_send"])
                    await client.subscribe(self.topics["call_dial"])
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._mqtt_inbound(client))
                        tg.create_task(self._mqtt_outbound(client))
            except aiomqtt.MqttError as e:
                logger.error(f"MQTT error: {e}, retry in {interval}s")
                self._mqtt_client = None
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"MQTT unexpected: {e}")
                self._mqtt_client = None
                await asyncio.sleep(interval)

    async def _mqtt_inbound(self, client):
        async for message in client.messages:
            topic = str(message.topic)
            try:
                data = json.loads(message.payload.decode("utf-8"))
            except Exception:
                logger.warning(f"Bad payload on {topic}")
                continue
            if topic == self.topics["sms_send"]:
                phone, text = data.get("to", ""), data.get("text", "")
                if phone and text:
                    await self.modem.send_sms(phone, text)
            elif topic == self.topics["call_dial"]:
                phone = data.get("to", "")
                if phone:
                    await self.modem.dial(phone)

    async def _mqtt_outbound(self, client):
        while True:
            topic, payload = await self._send_queue.get()
            try:
                await client.publish(topic, payload, qos=1)
            except Exception as e:
                logger.error(f"Publish error: {e}")
            finally:
                self._send_queue.task_done()

    async def _mqtt_publish(self, topic: str, payload: str):
        await self._send_queue.put((topic, payload))

    async def _publish_call(self, caller: str):
        trusted_flag = 1 if is_trusted(caller, self._trusted) else 0
        logger.info(f"Call from {caller} (trusted={trusted_flag})")
        payload = json.dumps({
            "from": caller, "action": "hangup",
            "trusted": trusted_flag,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }, ensure_ascii=False)
        await self._mqtt_publish(self.topics["call_inbox"], payload)

    async def _status_loop(self):
        interval = self.gw_cfg["status_interval"]
        while self._running:
            await asyncio.sleep(interval)
            if not self.at.connected:
                continue
            try:
                status = await self.modem.get_status()
                payload = json.dumps(status, ensure_ascii=False)
                await self._mqtt_publish(self.topics["status"], payload)
                logger.info(f"Status: online={status['online']}, signal={status.get('signal_dbm')}dBm, op={status.get('operator')}")
            except Exception as e:
                logger.error(f"Status error: {e}")

    # ── Run ───────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("GSM-MQTT Gateway starting...")
        await self._connect_serial()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._serial_reader())
            tg.create_task(self._serial_watchdog())
            tg.create_task(self._mqtt_loop())
            tg.create_task(self._status_loop())

    async def stop(self):
        logger.info("Shutting down...")
        self._running = False
        await self.at.disconnect()


# ─────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────

async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gateway_config.yaml"
    cfg = load_config(config_path)
    setup_logging(cfg["gateway"]["log_level"])

    gateway = GSMMQTTGateway(cfg)

    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(gateway.stop()))
        except NotImplementedError:
            pass

    try:
        await gateway.run()
    except* KeyboardInterrupt:
        pass
    finally:
        await gateway.stop()
        logger.info("Gateway stopped")


if __name__ == "__main__":
    asyncio.run(main())
