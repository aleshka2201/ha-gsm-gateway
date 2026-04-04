"""
GSM-MQTT Gateway for SIM800 USB stick
Home Assistant Addon — v1.3.0

Fixes vs 1.2.0:
  - SMS: AT+CMGS prompt '>' тепер читається в AT черзі (is_at_busy не скидається між кроками)
  - Reboot: чекає поки модем повністю завантажиться (до 30с) перед реінітом
  - trusted: парсинг через gen_config.py + parse_trusted_list захищений від всіх форматів
  - _connect_serial: перевіряє _running перед retry щоб не висіти після stop()
  - TaskGroup замінено на незалежні tasks — одна задача що впала не вбиває інші
"""

import asyncio
import json
import logging
import re
import signal
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiomqtt
import serial_asyncio
import yaml

LOG_FILE    = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"
CMD_FILE    = "/tmp/gsm_cmd.json"


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def setup_logging(level: str):
    log_level = getattr(logging, level.upper(), logging.INFO)
    fmt     = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=512*1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt, datefmt))
        handlers.append(fh)
    except Exception:
        pass
    logging.basicConfig(level=log_level, format=fmt, datefmt=datefmt, handlers=handlers)

logger = logging.getLogger("gsm_gateway")


# ─────────────────────────────────────────────
# Phone helpers
# ─────────────────────────────────────────────

def normalize_phone(raw: str) -> str:
    s = str(raw).strip().strip("'\"")
    s = re.sub(r"[\s\-\(\)]", "", s)
    s = re.sub(r"[^\d+]", "", s)
    if "+" in s:
        s = "+" + s.replace("+", "")
    return s


def phones_match(a: str, b: str) -> bool:
    na, nb = normalize_phone(a), normalize_phone(b)
    if na == nb:
        return True
    if na.lstrip("+") == nb.lstrip("+"):
        return True
    da = re.sub(r"\D", "", na)
    db = re.sub(r"\D", "", nb)
    if len(da) >= 9 and len(db) >= 9 and da[-9:] == db[-9:]:
        return True
    return False


def is_trusted(phone: str, trusted_list: list) -> bool:
    for t in trusted_list:
        if phones_match(phone, t):
            return True
    return False


def parse_trusted_list(raw) -> list[str]:
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    result = []
    for item in items:
        n = normalize_phone(str(item))
        if n:
            result.append(n)
    return result


# ─────────────────────────────────────────────
# SMS PDU
# ─────────────────────────────────────────────

def encode_ucs2(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def decode_ucs2(hex_str: str) -> str:
    try:
        return bytes.fromhex(hex_str).decode("utf-16-be")
    except Exception:
        return hex_str


def build_pdu(phone: str, text: str) -> tuple[str, int]:
    digits = re.sub(r"\D", "", phone)
    toa    = "91" if phone.startswith("+") else "81"
    padded = digits if len(digits) % 2 == 0 else digits + "F"
    encoded = "".join(padded[i+1] + padded[i] for i in range(0, len(padded), 2))
    phone_len    = hex(len(re.sub(r"\D", "", phone)))[2:].upper().zfill(2)
    text_encoded = encode_ucs2(text)
    udl          = hex(len(text) * 2)[2:].upper().zfill(2)
    pdu = "00" + "11" + "00" + phone_len + toa + encoded + "00" + "08" + "AA" + udl + text_encoded
    return pdu, len(pdu) // 2 - 1


def decode_incoming_pdu(pdu: str) -> tuple[str, str]:
    idx = 0

    def read(n: int) -> str:
        nonlocal idx
        v = pdu[idx:idx+n]; idx += n; return v

    smsc_len      = int(read(2), 16)
    read(smsc_len * 2)
    pdu_type_byte = int(read(2), 16)
    oa_len        = int(read(2), 16)
    oa_type       = int(read(2), 16)
    oa_raw        = read((oa_len + 1) // 2 * 2)

    if oa_type in (0x91, 0x81):
        phone = ""
        for i in range(0, len(oa_raw) - 1, 2):
            phone += oa_raw[i+1]
            if oa_raw[i] != "F":
                phone += oa_raw[i]
        if oa_type == 0x91:
            phone = "+" + phone
    else:
        phone = oa_raw

    read(2)  # PID
    dcs    = int(read(2), 16)
    vp_fmt = (pdu_type_byte >> 3) & 0x03
    if vp_fmt == 0x02:   read(2)
    elif vp_fmt in (1,3): read(14)
    read(14)  # SCTS
    udl = int(read(2), 16)

    if dcs & 0x08:
        text = decode_ucs2(pdu[idx:])
    elif (dcs >> 2) & 0x03 == 0x01:
        text = bytes.fromhex(pdu[idx:]).decode("latin-1", errors="replace")
    else:
        text = _decode_gsm7(pdu[idx:], udl)
    return phone, text


def _decode_gsm7(hex_str: str, num_chars: int) -> str:
    T = ("@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
         "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u03b8\u039e\x1b\u00c6\u00e6\u00df\u00c9"
         " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
         "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
         "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0")
    try:
        bits = int.from_bytes(bytes.fromhex(hex_str), "little")
        return "".join(T[(bits >> (i*7)) & 0x7F] if (bits >> (i*7)) & 0x7F < len(T) else "?" for i in range(num_chars))
    except Exception:
        return hex_str


# ─────────────────────────────────────────────
# ATSerial — queue-based, single pump reader
# ─────────────────────────────────────────────

class ATSerial:
    def __init__(self, port: str, baudrate: int, at_timeout: float):
        self.port        = port
        self.baudrate    = baudrate
        self.at_timeout  = at_timeout
        self._reader     = None
        self._writer     = None
        self._write_lock = asyncio.Lock()
        self._at_lock    = asyncio.Lock()
        self._at_queue:  asyncio.Queue = asyncio.Queue()
        self._urc_queue: asyncio.Queue = asyncio.Queue()
        self._is_at_busy   = False
        self._connected    = False
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
        # Очищаємо черги щоб не було сміття при наступному connect
        for q in (self._at_queue, self._urc_queue):
            while not q.empty():
                try: q.get_nowait()
                except Exception: break

    async def _pump(self):
        while self._connected:
            try:
                raw     = await asyncio.wait_for(self._reader.readline(), timeout=2.0)
                decoded = raw.decode(errors="replace").strip()
                if not decoded:
                    continue
                self._last_activity = time.monotonic()
                logger.debug(f"<< {decoded!r}")
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
        if not self._connected:
            raise ConnectionError("Serial not connected")
        timeout = timeout or self.at_timeout
        async with self._at_lock:
            while not self._at_queue.empty():
                try: self._at_queue.get_nowait()
                except Exception: break
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
                        if decoded.startswith(("+CMS ERROR", "+CME ERROR")):
                            break
                self._last_activity = time.monotonic()
                return "\n".join(lines)
            except asyncio.TimeoutError:
                logger.warning(f"AT timeout: {cmd!r}")
                raise
            finally:
                self._is_at_busy = False

    async def send_sms_full(self, pdu_len: int, pdu: str) -> bool:
        """
        Відправляє SMS як єдину AT-транзакцію.
        AT+CMGS= і PDU дані — під одним _at_lock без відпускання між кроками.
        Це гарантує що промпт '>' попадає в AT чергу а не в URC.
        """
        if not self._connected:
            raise ConnectionError("Serial not connected")

        async with self._at_lock:
            # Очищаємо AT чергу
            while not self._at_queue.empty():
                try: self._at_queue.get_nowait()
                except Exception: break

            self._is_at_busy = True
            try:
                # Крок 1: надсилаємо AT+CMGS=N
                async with self._write_lock:
                    self._writer.write(f"AT+CMGS={pdu_len}\r\n".encode())
                    await self._writer.drain()
                self._last_activity = time.monotonic()

                # Чекаємо промпт '>'
                async with asyncio.timeout(10):
                    while True:
                        line = await self._at_queue.get()
                        logger.debug(f"SMS step1 << {line!r}")
                        if ">" in line:
                            break
                        if line in ("ERROR",) or line.startswith("+CMS ERROR"):
                            logger.error(f"CMGS rejected: {line}")
                            return False

                # Крок 2: надсилаємо PDU + Ctrl-Z (все ще під тим самим lock)
                async with self._write_lock:
                    self._writer.write((pdu + "\x1A").encode())
                    await self._writer.drain()

                # Чекаємо OK або +CMGS
                lines = []
                async with asyncio.timeout(30):
                    while True:
                        line = await self._at_queue.get()
                        logger.debug(f"SMS step2 << {line!r}")
                        if line:
                            lines.append(line)
                        if line in ("OK", "ERROR") or line.startswith(("+CMS ERROR", "+CMGS")):
                            break

                full = "\n".join(lines)
                return "OK" in full or "+CMGS" in full

            except asyncio.TimeoutError:
                logger.error("SMS send timeout")
                return False
            finally:
                self._is_at_busy = False

    async def read_urc(self, timeout: float = 1.0) -> str | None:
        if not self._connected:
            return None
        try:
            return await asyncio.wait_for(self._urc_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            logger.error(f"read_urc error: {e}")
            return None


# ─────────────────────────────────────────────
# ModemManager
# ─────────────────────────────────────────────

class ModemManager:
    def __init__(self, at: ATSerial):
        self.at = at

    async def init_modem(self):
        for cmd, expect in [
            ("AT",                "OK"),
            ("ATE0",              "OK"),
            ("AT+CMGF=0",         "OK"),
            ("AT+CNMI=2,2,0,0,0", "OK"),
            ("AT+CLIP=1",         "OK"),
            ("AT+CLTS=1",         "OK"),
            ('AT+CSCS="UCS2"',    "OK"),
        ]:
            try:
                await self.at.send_at(cmd, wait_for=expect)
            except Exception as e:
                logger.warning(f"Init {cmd!r} failed: {e}")
        logger.info("Modem initialized")

    async def wait_for_ready(self, timeout: float = 30.0) -> bool:
        """Чекає поки модем відповідає на AT після ребуту."""
        logger.info(f"Waiting for modem ready (max {timeout}s)...")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                resp = await self.at.send_at("AT", wait_for="OK", timeout=3.0)
                if "OK" in resp:
                    logger.info("Modem is ready")
                    return True
            except Exception:
                pass
            await asyncio.sleep(2)
        logger.error("Modem did not become ready in time")
        return False

    async def get_status(self) -> dict:
        status: dict = {
            "timestamp":    datetime.utcnow().isoformat() + "Z",
            "online":       False,
            "signal_rssi":  None,
            "signal_dbm":   None,
            "operator":     None,
            "sim_ready":    False,
            "registration": None,
        }
        try:
            r = await self.at.send_at("AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),", r)
            if m:
                rssi = int(m.group(1))
                status["signal_rssi"] = rssi
                if rssi not in (0, 99):
                    status["signal_dbm"] = -113 + rssi * 2
                    status["online"] = True

            r = await self.at.send_at("AT+CPIN?")
            status["sim_ready"] = "READY" in r

            r = await self.at.send_at("AT+CREG?")
            m = re.search(r"\+CREG:\s*\d+,(\d+)", r)
            if m:
                status["registration"] = {
                    "0": "not_registered", "1": "registered_home",
                    "2": "searching",      "3": "denied",
                    "5": "registered_roaming",
                }.get(m.group(1), m.group(1))

            r = await self.at.send_at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', r)
            if m:
                try:    status["operator"] = decode_ucs2(m.group(1))
                except: status["operator"] = m.group(1)

        except Exception as e:
            logger.warning(f"get_status error: {e}")

        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False)
        except Exception:
            pass
        return status

    async def send_sms(self, phone: str, text: str) -> bool:
        try:
            pdu, pdu_len = build_pdu(phone, text)
            logger.info(f"Sending SMS to {phone}, PDU len={pdu_len}")
            ok = await self.at.send_sms_full(pdu_len, pdu)
            if ok:
                logger.info(f"SMS sent OK to {phone}")
            else:
                logger.error(f"SMS send FAILED to {phone}")
            return ok
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

    async def reboot(self):
        """
        Програмний ребут модему.
        AT+CFUN=1,1 не завжди повертає OK перед ребутом — ігноруємо помилку.
        """
        logger.info("Sending AT+CFUN=1,1 (modem reboot)...")
        try:
            await asyncio.wait_for(
                self.at.send_at("AT+CFUN=1,1", wait_for="OK", timeout=3.0),
                timeout=4.0
            )
        except Exception:
            pass  # модем може одразу перезавантажитись не відповівши


# ─────────────────────────────────────────────
# URC parser
# ─────────────────────────────────────────────

class URCParser:
    def parse(self, line: str) -> dict | None:
        if line.startswith("+CMT:"):
            return {"type": "sms_header", "raw": line}
        m = re.match(r'\+CLIP:\s*"([^"]*)"', line)
        if m:
            caller = m.group(1)
            try: caller = decode_ucs2(caller)
            except: pass
            return {"type": "call", "caller": caller}
        if line == "RING":
            return {"type": "ring"}
        if line in ("NO CARRIER", "BUSY", "NO ANSWER"):
            return {"type": "call_ended", "reason": line}
        return None


# ─────────────────────────────────────────────
# Gateway
# ─────────────────────────────────────────────

class GSMMQTTGateway:
    def __init__(self, cfg: dict):
        self.cfg        = cfg
        self.topics     = cfg["topics"]
        self.serial_cfg = cfg["serial"]
        self.mqtt_cfg   = cfg["mqtt"]
        self.gw_cfg     = cfg["gateway"]

        self.at    = ATSerial(
            port       = self.serial_cfg["port"],
            baudrate   = self.serial_cfg["baudrate"],
            at_timeout = self.gw_cfg["at_command_timeout"],
        )
        self.modem = ModemManager(self.at)
        self.urc   = URCParser()

        self._send_queue: asyncio.Queue = asyncio.Queue()
        self._running = False

        raw_trusted   = self.gw_cfg.get("trusted_numbers", [])
        self._trusted = parse_trusted_list(raw_trusted)
        logger.info(f"Trusted numbers loaded ({len(self._trusted)}): {self._trusted}")

    # ── Serial ────────────────────────────────

    async def _connect_serial(self):
        while self._running:
            try:
                await self.at.connect()
                await self.modem.init_modem()
                return
            except Exception as e:
                logger.error(f"Serial connect failed: {e}, retry in 5s")
                try:
                    await self.at.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(5)

    async def _serial_watchdog(self):
        wd_timeout = self.serial_cfg["watchdog_timeout"]
        while self._running:
            await asyncio.sleep(10)
            if not self.at.connected:
                continue
            idle = time.monotonic() - self.at.last_activity
            if idle > wd_timeout:
                logger.warning(f"Watchdog: {idle:.0f}s idle — reconnecting serial")
                await self.at.disconnect()
                await asyncio.sleep(2)
                await self._connect_serial()

    async def _serial_reader(self):
        sms_header: str | None = None
        while self._running:
            if not self.at.connected:
                await asyncio.sleep(1)
                continue
            try:
                line = await self.at.read_urc(timeout=1.0)
                if not line:
                    continue
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
                    await self._publish_call(event["caller"])
                    await asyncio.sleep(0.3)
                    await self.modem.hangup()
                elif event["type"] == "ring":
                    logger.debug("RING")
                elif event["type"] == "call_ended":
                    logger.debug(f"Call ended: {event['reason']}")
            except Exception as e:
                logger.error(f"Serial reader error: {e}")
                await asyncio.sleep(1)

    async def _handle_sms_pdu(self, header: str, pdu_line: str):
        try:
            phone, text = decode_incoming_pdu(pdu_line)
            trusted = 1 if is_trusted(phone, self._trusted) else 0
            logger.info(f"SMS from {phone} (trusted={trusted}): {text[:60]}")
            await self._mqtt_publish(self.topics["sms_inbox"], json.dumps({
                "from": phone, "text": text,
                "trusted": trusted,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }, ensure_ascii=False))
        except Exception as e:
            logger.error(f"SMS PDU decode error: {e} | raw={pdu_line!r}")

    async def _publish_call(self, caller: str):
        trusted = 1 if is_trusted(caller, self._trusted) else 0
        logger.info(f"Call from {caller} (trusted={trusted})")
        await self._mqtt_publish(self.topics["call_inbox"], json.dumps({
            "from": caller, "action": "hangup",
            "trusted": trusted,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }, ensure_ascii=False))

    # ── Web UI commands ────────────────────────

    async def _webui_cmd_loop(self):
        while self._running:
            await asyncio.sleep(1)
            try:
                p = Path(CMD_FILE)
                if not p.exists():
                    continue
                raw = p.read_text(encoding="utf-8").strip()
                p.unlink(missing_ok=True)
                if not raw:
                    continue
                cmd    = json.loads(raw)
                action = cmd.get("action", "")

                if action == "reboot_modem":
                    logger.info("=== MODEM REBOOT REQUESTED ===")
                    await self.modem.reboot()
                    logger.info("Waiting 8s for modem to restart...")
                    await asyncio.sleep(8)
                    await self.at.disconnect()
                    await asyncio.sleep(2)
                    # Реконектимось і чекаємо поки модем готовий
                    await self._connect_serial()
                    await self.modem.wait_for_ready(timeout=30)
                    logger.info("=== MODEM REBOOT DONE ===")

                elif action == "send_sms":
                    phone = cmd.get("to", "").strip()
                    text  = cmd.get("text", "").strip()
                    if phone and text:
                        logger.info(f"Web UI: send SMS → {phone}")
                        await self.modem.send_sms(phone, text)
                    else:
                        logger.warning("Web UI send_sms: missing 'to' or 'text'")

            except json.JSONDecodeError as e:
                logger.warning(f"Web UI cmd JSON error: {e}")
            except Exception as e:
                logger.error(f"Web UI cmd loop error: {e}")

    # ── MQTT ──────────────────────────────────

    async def _mqtt_loop(self):
        interval = self.mqtt_cfg.get("reconnect_interval", 5)
        while self._running:
            try:
                logger.info(f"MQTT connecting {self.mqtt_cfg['host']}:{self.mqtt_cfg['port']}")
                async with aiomqtt.Client(
                    hostname  = self.mqtt_cfg["host"],
                    port      = self.mqtt_cfg["port"],
                    username  = self.mqtt_cfg["username"] or None,
                    password  = self.mqtt_cfg["password"] or None,
                    identifier= self.mqtt_cfg["client_id"],
                    keepalive = self.mqtt_cfg["keepalive"],
                ) as client:
                    logger.info("MQTT connected")
                    await client.subscribe(self.topics["sms_send"])
                    await client.subscribe(self.topics["call_dial"])
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._mqtt_inbound(client))
                        tg.create_task(self._mqtt_outbound(client))
            except aiomqtt.MqttError as e:
                logger.error(f"MQTT error: {e}, retry in {interval}s")
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"MQTT unexpected: {e}")
                await asyncio.sleep(interval)

    async def _mqtt_inbound(self, client):
        async for message in client.messages:
            topic = str(message.topic)
            try:
                data = json.loads(message.payload.decode("utf-8"))
            except Exception:
                logger.warning(f"Bad MQTT payload on {topic}")
                continue
            if topic == self.topics["sms_send"]:
                phone = data.get("to", "").strip()
                text  = data.get("text", "").strip()
                if phone and text:
                    await self.modem.send_sms(phone, text)
                else:
                    logger.warning("sms_send: missing 'to' or 'text'")
            elif topic == self.topics["call_dial"]:
                phone = data.get("to", "").strip()
                if phone:
                    await self.modem.dial(phone)

    async def _mqtt_outbound(self, client):
        while True:
            topic, payload = await self._send_queue.get()
            try:
                await client.publish(topic, payload, qos=1)
            except Exception as e:
                logger.error(f"MQTT publish error: {e}")
            finally:
                self._send_queue.task_done()

    async def _mqtt_publish(self, topic: str, payload: str):
        await self._send_queue.put((topic, payload))

    # ── Status ────────────────────────────────

    async def _status_loop(self):
        interval = self.gw_cfg["status_interval"]
        while self._running:
            await asyncio.sleep(interval)
            if not self.at.connected:
                continue
            try:
                status  = await self.modem.get_status()
                payload = json.dumps(status, ensure_ascii=False)
                await self._mqtt_publish(self.topics["status"], payload)
                logger.info(
                    f"Status: online={status['online']}, "
                    f"signal={status.get('signal_dbm')}dBm, "
                    f"op={status.get('operator')}, reg={status.get('registration')}"
                )
            except Exception as e:
                logger.error(f"Status loop error: {e}")

    # ── Run / Stop ────────────────────────────

    async def run(self):
        self._running = True
        logger.info("GSM-MQTT Gateway v1.3.0 starting...")
        logger.info(f"Serial: {self.serial_cfg['port']} @ {self.serial_cfg['baudrate']}")
        logger.info(f"MQTT:   {self.mqtt_cfg['host']}:{self.mqtt_cfg['port']}")

        await self._connect_serial()

        tasks = [
            asyncio.create_task(self._serial_reader(),   name="serial_reader"),
            asyncio.create_task(self._serial_watchdog(), name="serial_watchdog"),
            asyncio.create_task(self._mqtt_loop(),       name="mqtt_loop"),
            asyncio.create_task(self._status_loop(),     name="status_loop"),
            asyncio.create_task(self._webui_cmd_loop(),  name="webui_cmd"),
        ]
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Fatal task error: {e}")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

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
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(gateway.stop()))
        except NotImplementedError:
            pass

    try:
        await gateway.run()
    except KeyboardInterrupt:
        pass
    finally:
        await gateway.stop()
        logger.info("Gateway stopped")


if __name__ == "__main__":
    asyncio.run(main())
