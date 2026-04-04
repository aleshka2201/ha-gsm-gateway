"""
GSM-MQTT Gateway — v1.4.0
Home Assistant Addon for SIM800 USB stick.

Нова архітектура serial (vs v1.3):
──────────────────────────────────
Замість pump+черга+флаг — простий підхід:
  • Один asyncio.Lock (_serial_lock) захищає serial порт цілком.
  • send_at() захоплює лок і читає відповідь напряму з reader.
  • _urc_loop() намагається взяти лок тільки коли він вільний.
  • При connect/disconnect — скидаємо стан і чекаємо 10с щоб
    модем встиг завантажитись (вирішує проблему після ребуту).

Це прибирає всю складність з чергами, флагами і race condition.
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

# ═══════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════

def setup_logging(level: str) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=512*1024, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
        handlers.append(fh)
    except Exception:
        pass
    logging.basicConfig(level=lvl, format=fmt, datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)

log = logging.getLogger("gsm")

# ═══════════════════════════════════════════════
# Phone helpers
# ═══════════════════════════════════════════════

def norm_phone(raw: str) -> str:
    s = str(raw).strip().strip("'\"")
    s = re.sub(r"[\s\(\)\-]", "", s)
    s = re.sub(r"[^\d+]", "", s)
    return ("+" + s.replace("+", "")) if "+" in s else s

def phones_eq(a: str, b: str) -> bool:
    na, nb = norm_phone(a), norm_phone(b)
    if na == nb: return True
    if na.lstrip("+") == nb.lstrip("+"): return True
    da, db = re.sub(r"\D","",na), re.sub(r"\D","",nb)
    return len(da) >= 9 and len(db) >= 9 and da[-9:] == db[-9:]

def is_trusted(phone: str, lst: list[str]) -> bool:
    return any(phones_eq(phone, t) for t in lst)

def parse_trusted(raw) -> list[str]:
    if not raw: return []
    items = raw if isinstance(raw, list) else [raw]
    return [n for n in (norm_phone(str(i)) for i in items) if n]

# ═══════════════════════════════════════════════
# SMS PDU
# ═══════════════════════════════════════════════

def ucs2_enc(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()

def ucs2_dec(h: str) -> str:
    try:    return bytes.fromhex(h).decode("utf-16-be")
    except: return h

def build_pdu(phone: str, text: str) -> tuple[str, int]:
    d = re.sub(r"\D", "", phone)
    toa = "91" if phone.startswith("+") else "81"
    p = d if len(d)%2==0 else d+"F"
    pe = "".join(p[i+1]+p[i] for i in range(0,len(p),2))
    pl = hex(len(d))[2:].upper().zfill(2)
    te = ucs2_enc(text)
    ul = hex(len(text)*2)[2:].upper().zfill(2)
    pdu = "00"+"11"+"00"+pl+toa+pe+"00"+"08"+"AA"+ul+te
    return pdu, len(pdu)//2-1

def decode_pdu(pdu: str) -> tuple[str, str]:
    pos = 0
    def rd(n):
        nonlocal pos; v=pdu[pos:pos+n]; pos+=n; return v
    sl = int(rd(2),16); rd(sl*2)
    pt = int(rd(2),16)
    al = int(rd(2),16); at = int(rd(2),16)
    ar = rd((al+1)//2*2)
    if at in (0x91,0x81):
        ph=""
        for i in range(0,len(ar)-1,2):
            ph+=ar[i+1]
            if ar[i]!="F": ph+=ar[i]
        if at==0x91: ph="+"+ph
    else: ph=ar
    rd(2); dcs=int(rd(2),16)
    vf=(pt>>3)&3
    if vf==2: rd(2)
    elif vf in (1,3): rd(14)
    rd(14); udl=int(rd(2),16)
    if dcs&0x08:    txt=ucs2_dec(pdu[pos:])
    elif (dcs>>2)&3==1: txt=bytes.fromhex(pdu[pos:]).decode("latin-1","replace")
    else:           txt=_gsm7(pdu[pos:],udl)
    return ph, txt

def _gsm7(h: str, n: int) -> str:
    T=("@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
       "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u03b8\u039e\x1b\u00c6\u00e6\u00df\u00c9"
       " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
       "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
       "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0")
    try:
        b=int.from_bytes(bytes.fromhex(h),"little")
        return "".join(T[(b>>(i*7))&0x7f] if (b>>(i*7))&0x7f<len(T) else "?" for i in range(n))
    except: return h

# ═══════════════════════════════════════════════
# Serial layer
# ═══════════════════════════════════════════════

class SerialPort:
    """
    Проста абстракція над serial портом.
    Один Lock (_lock) захищає всі операції читання і запису.
    send_at() і send_sms() захоплюють лок повністю на час транзакції.
    urc_reader() намагається взяти лок з timeout — якщо зайнятий, пропускає.
    """

    TERM = {"OK", "ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"}

    def __init__(self, port: str, baud: int, at_timeout: float):
        self.port       = port
        self.baud       = baud
        self.at_timeout = at_timeout
        self._lock      = asyncio.Lock()
        self._r         = None
        self._w         = None
        self._ok        = False
        self._last_rx   = time.monotonic()

    @property
    def alive(self) -> bool:
        return self._ok

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_rx

    async def open(self) -> None:
        self._r, self._w = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baud
        )
        self._ok   = True
        self._last_rx = time.monotonic()
        log.info(f"Serial opened: {self.port}")

    async def close(self) -> None:
        self._ok = False
        if self._w:
            try: self._w.close(); await self._w.wait_closed()
            except: pass
        self._r = self._w = None
        log.info("Serial closed")

    # ── internal helpers ──────────────────────

    async def _readline(self, timeout: float) -> str:
        """Читає один рядок з таймаутом. Не захоплює лок."""
        raw = await asyncio.wait_for(self._r.readline(), timeout=timeout)
        decoded = raw.decode(errors="replace").strip()
        if decoded:
            self._last_rx = time.monotonic()
        return decoded

    async def _write(self, data: bytes) -> None:
        self._w.write(data)
        await self._w.drain()

    # ── AT command ────────────────────────────

    async def send_at(self, cmd: str, wait: str = "OK",
                      timeout: float | None = None) -> str:
        """
        Надсилає AT команду, повертає всю відповідь до термінального рядка.
        Захоплює _lock на весь час виконання.
        """
        if not self._ok:
            raise ConnectionError("Serial not open")
        t = timeout or self.at_timeout
        async with self._lock:
            log.debug(f">> {cmd!r}")
            await self._write(f"{cmd}\r\n".encode())
            lines: list[str] = []
            try:
                async with asyncio.timeout(t):
                    while True:
                        line = await self._readline(t)
                        if not line: continue
                        log.debug(f"<< {line!r}")
                        lines.append(line)
                        if line in (wait, *self.TERM): break
                        if line.startswith(("+CMS ERROR","+CME ERROR")): break
            except asyncio.TimeoutError:
                log.warning(f"AT timeout: {cmd!r}")
                raise
            return "\n".join(lines)

    async def send_sms(self, pdu_len: int, pdu: str) -> bool:
        """
        Повна SMS транзакція під одним локом:
        AT+CMGS= → чекає '>' → PDU+Ctrl-Z → чекає OK/+CMGS
        """
        if not self._ok:
            raise ConnectionError("Serial not open")
        async with self._lock:
            try:
                # Крок 1
                log.debug(f">> AT+CMGS={pdu_len}")
                await self._write(f"AT+CMGS={pdu_len}\r\n".encode())
                async with asyncio.timeout(10):
                    while True:
                        line = await self._readline(10)
                        log.debug(f"<< {line!r}")
                        if ">" in line: break
                        if line.startswith("+CMS ERROR") or line == "ERROR":
                            log.error(f"CMGS rejected: {line}")
                            return False

                # Крок 2
                log.debug(f">> [PDU {len(pdu)} chars] + Ctrl-Z")
                await self._write((pdu + "\x1A").encode())
                lines: list[str] = []
                async with asyncio.timeout(30):
                    while True:
                        line = await self._readline(30)
                        if not line: continue
                        log.debug(f"<< {line!r}")
                        lines.append(line)
                        if line in ("OK","ERROR") or line.startswith(("+CMS ERROR","+CMGS")):
                            break
                return "OK" in lines or any(l.startswith("+CMGS") for l in lines)

            except asyncio.TimeoutError:
                log.error("SMS send timeout")
                return False

    async def try_read_urc(self, timeout: float = 0.5) -> str | None:
        """
        Намагається прочитати один URC рядок БЕЗ блокування.
        Якщо _lock зайнятий (send_at виконується) — повертає None негайно.
        """
        if not self._ok: return None
        if self._lock.locked(): return None
        try:
            async with asyncio.timeout(timeout):
                # acquire lock з timeout=0 — не чекаємо якщо зайнятий
                acquired = await asyncio.wait_for(self._lock.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return None

        # Якщо не вдалось — значить lock зайнятий
        # acquired буде True якщо вдалось
        try:
            return await self._readline(timeout)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            log.error(f"URC read error: {e}")
            return None
        finally:
            self._lock.release()

# ═══════════════════════════════════════════════
# Modem
# ═══════════════════════════════════════════════

class Modem:
    def __init__(self, serial: SerialPort):
        self.s = serial

    async def init(self) -> None:
        cmds = [
            ("AT",                "OK"),
            ("ATE0",              "OK"),
            ("AT+CMGF=0",         "OK"),
            ("AT+CNMI=2,2,0,0,0", "OK"),
            ("AT+CLIP=1",         "OK"),
            ("AT+CLTS=1",         "OK"),
            ('AT+CSCS="UCS2"',    "OK"),
        ]
        for cmd, exp in cmds:
            try:
                await self.s.send_at(cmd, wait=exp)
            except Exception as e:
                log.warning(f"Init {cmd!r} failed: {e}")
        log.info("Modem initialized")

    async def ping(self, retries: int = 5, delay: float = 3.0) -> bool:
        """Перевіряє чи модем відповідає. Використовується після ребуту."""
        for i in range(retries):
            try:
                r = await self.s.send_at("AT", wait="OK", timeout=3.0)
                if "OK" in r:
                    log.info(f"Modem ping OK (attempt {i+1})")
                    return True
            except Exception:
                pass
            log.debug(f"Ping attempt {i+1}/{retries} failed, waiting {delay}s...")
            await asyncio.sleep(delay)
        return False

    async def status(self) -> dict:
        s: dict = {"timestamp": datetime.utcnow().isoformat()+"Z",
                   "online": False, "signal_rssi": None, "signal_dbm": None,
                   "operator": None, "sim_ready": False, "registration": None}
        try:
            r = await self.s.send_at("AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),", r)
            if m:
                rssi = int(m.group(1))
                s["signal_rssi"] = rssi
                if rssi not in (0, 99):
                    s["signal_dbm"] = -113 + rssi*2
                    s["online"] = True

            r = await self.s.send_at("AT+CPIN?")
            s["sim_ready"] = "READY" in r

            r = await self.s.send_at("AT+CREG?")
            m = re.search(r"\+CREG:\s*\d+,(\d+)", r)
            if m:
                s["registration"] = {"0":"not_registered","1":"registered_home",
                    "2":"searching","3":"denied","5":"registered_roaming"}.get(m.group(1), m.group(1))

            r = await self.s.send_at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', r)
            if m:
                try: s["operator"] = ucs2_dec(m.group(1))
                except: s["operator"] = m.group(1)
        except Exception as e:
            log.warning(f"Status query error: {e}")

        try:
            Path(STATUS_FILE).write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        except: pass
        return s

    async def send_sms(self, phone: str, text: str) -> bool:
        try:
            pdu, pdu_len = build_pdu(phone, text)
            log.info(f"Sending SMS to {phone} (pdu_len={pdu_len})")
            ok = await self.s.send_sms(pdu_len, pdu)
            log.info(f"SMS to {phone}: {'OK' if ok else 'FAILED'}")
            return ok
        except Exception as e:
            log.error(f"send_sms error: {e}")
            return False

    async def hangup(self) -> None:
        try: await self.s.send_at("ATH")
        except: pass

    async def dial(self, phone: str) -> bool:
        try:
            r = await self.s.send_at(f"ATD{phone};", wait="OK", timeout=15)
            return "OK" in r
        except Exception as e:
            log.error(f"Dial error: {e}"); return False

    async def soft_reboot(self) -> None:
        """
        Програмний ребут: AT+CFUN=1,1.
        Модем може не відповісти OK — це нормально.
        """
        log.info("Soft reboot: AT+CFUN=1,1")
        try:
            await asyncio.wait_for(
                self.s.send_at("AT+CFUN=1,1", wait="OK", timeout=3),
                timeout=4
            )
        except Exception:
            pass  # очікувана поведінка — модем одразу ребутиться

# ═══════════════════════════════════════════════
# URC parser
# ═══════════════════════════════════════════════

def parse_urc(line: str) -> dict | None:
    if line.startswith("+CMT:"): return {"t":"sms_hdr","raw":line}
    m = re.match(r'\+CLIP:\s*"([^"]*)"', line)
    if m:
        c = m.group(1)
        try: c = ucs2_dec(c)
        except: pass
        return {"t":"call","caller":c}
    if line == "RING":              return {"t":"ring"}
    if line in ("NO CARRIER","BUSY","NO ANSWER"): return {"t":"hangup","r":line}
    return None

# ═══════════════════════════════════════════════
# Gateway
# ═══════════════════════════════════════════════

class Gateway:
    def __init__(self, cfg: dict):
        self.cfg     = cfg
        self.topics  = cfg["topics"]
        self.gw_cfg  = cfg["gateway"]
        self.mqtt_cfg= cfg["mqtt"]

        self.serial  = SerialPort(
            port       = cfg["serial"]["port"],
            baud       = cfg["serial"]["baudrate"],
            at_timeout = cfg["gateway"]["at_command_timeout"],
        )
        self.modem   = Modem(self.serial)
        self._mq_q:  asyncio.Queue = asyncio.Queue()
        self._running = False
        self._trusted = parse_trusted(self.gw_cfg.get("trusted_numbers", []))
        log.info(f"Trusted numbers: {self._trusted}")

    # ── Serial connect / reconnect ─────────────

    async def _open_serial(self) -> None:
        """Відкриває serial і ініціалізує модем. Повторює до успіху."""
        while self._running:
            try:
                if self.serial.alive:
                    await self.serial.close()
                await asyncio.sleep(1)
                await self.serial.open()
                await self.modem.init()
                return
            except Exception as e:
                log.error(f"Serial open failed: {e}  — retry in 5s")
                try: await self.serial.close()
                except: pass
                await asyncio.sleep(5)

    async def _reopen_after_reboot(self) -> None:
        """
        Спеціальна процедура після soft_reboot:
        1. Закриваємо serial.
        2. Чекаємо 10с щоб модем встиг перезавантажитись.
        3. Відкриваємо serial знову.
        4. Пінгуємо з retry — чекаємо поки готовий.
        5. Ініціалізуємо.
        """
        log.info("Reboot sequence: closing serial...")
        try: await self.serial.close()
        except: pass

        log.info("Reboot sequence: waiting 10s for modem to boot...")
        await asyncio.sleep(10)

        log.info("Reboot sequence: reopening serial...")
        while self._running:
            try:
                await self.serial.open()
                break
            except Exception as e:
                log.error(f"Serial reopen failed: {e} — retry in 3s")
                await asyncio.sleep(3)

        log.info("Reboot sequence: pinging modem...")
        if await self.modem.ping(retries=10, delay=3):
            await self.modem.init()
            log.info("Reboot sequence: DONE — modem ready")
        else:
            log.error("Reboot sequence: modem did not respond — will retry via watchdog")

    # ── Watchdog ──────────────────────────────

    async def _watchdog(self) -> None:
        wd = self.cfg["serial"]["watchdog_timeout"]
        while self._running:
            await asyncio.sleep(15)
            if not self.serial.alive: continue
            idle = self.serial.idle_seconds
            if idle > wd:
                log.warning(f"Watchdog: {idle:.0f}s idle — reconnecting")
                await self._open_serial()

    # ── URC loop ──────────────────────────────

    async def _urc_loop(self) -> None:
        """
        Читає URC рядки коли serial лок вільний.
        Використовує try_read_urc() — не конкурує з send_at().
        """
        sms_hdr: str | None = None
        while self._running:
            if not self.serial.alive:
                await asyncio.sleep(1)
                continue
            try:
                line = await self.serial.try_read_urc(timeout=0.5)
                if not line:
                    continue

                if sms_hdr is not None:
                    await self._on_sms(sms_hdr, line)
                    sms_hdr = None
                    continue

                ev = parse_urc(line)
                if not ev: continue

                if   ev["t"] == "sms_hdr": sms_hdr = ev["raw"]
                elif ev["t"] == "call":
                    await self._on_call(ev["caller"])
                    await asyncio.sleep(0.3)
                    await self.modem.hangup()
                elif ev["t"] == "ring":    log.debug("RING")
                elif ev["t"] == "hangup":  log.debug(f"Call ended: {ev['r']}")

            except Exception as e:
                log.error(f"URC loop error: {e}")
                await asyncio.sleep(1)

    async def _on_sms(self, hdr: str, pdu: str) -> None:
        try:
            phone, text = decode_pdu(pdu)
            trusted = 1 if is_trusted(phone, self._trusted) else 0
            log.info(f"SMS from {phone} trusted={trusted}: {text[:60]}")
            await self._pub(self.topics["sms_inbox"], json.dumps(
                {"from":phone,"text":text,"trusted":trusted,
                 "timestamp":datetime.utcnow().isoformat()+"Z"},
                ensure_ascii=False))
        except Exception as e:
            log.error(f"SMS decode error: {e} | pdu={pdu!r}")

    async def _on_call(self, caller: str) -> None:
        trusted = 1 if is_trusted(caller, self._trusted) else 0
        log.info(f"Call from {caller} trusted={trusted}")
        await self._pub(self.topics["call_inbox"], json.dumps(
            {"from":caller,"action":"hangup","trusted":trusted,
             "timestamp":datetime.utcnow().isoformat()+"Z"},
            ensure_ascii=False))

    # ── Status loop ───────────────────────────

    async def _status_loop(self) -> None:
        interval = self.gw_cfg["status_interval"]
        while self._running:
            await asyncio.sleep(interval)
            if not self.serial.alive: continue
            try:
                s = await self.modem.status()
                await self._pub(self.topics["status"],
                                json.dumps(s, ensure_ascii=False))
                log.info(f"Status: online={s['online']} "
                         f"signal={s.get('signal_dbm')}dBm "
                         f"op={s.get('operator')} reg={s.get('registration')}")
            except Exception as e:
                log.error(f"Status loop error: {e}")

    # ── WebUI commands ────────────────────────

    async def _cmd_loop(self) -> None:
        """Polls CMD_FILE for commands from Web UI."""
        while self._running:
            await asyncio.sleep(1)
            p = Path(CMD_FILE)
            if not p.exists(): continue
            try:
                raw = p.read_text(encoding="utf-8").strip()
                p.unlink(missing_ok=True)
                if not raw: continue
                cmd = json.loads(raw)
            except Exception as e:
                log.warning(f"CMD parse error: {e}"); continue

            action = cmd.get("action","")

            if action == "reboot_modem":
                log.info("=== MODEM REBOOT ===")
                if self.serial.alive:
                    await self.modem.soft_reboot()
                await self._reopen_after_reboot()

            elif action == "send_sms":
                to   = cmd.get("to","").strip()
                text = cmd.get("text","").strip()
                if to and text:
                    await self.modem.send_sms(to, text)
                else:
                    log.warning("send_sms: missing to/text")

    # ── MQTT ──────────────────────────────────

    async def _mqtt_loop(self) -> None:
        mc = self.mqtt_cfg
        ri = mc.get("reconnect_interval", 5)
        while self._running:
            try:
                log.info(f"MQTT connecting {mc['host']}:{mc['port']}")
                async with aiomqtt.Client(
                    hostname  = mc["host"],
                    port      = mc["port"],
                    username  = mc["username"] or None,
                    password  = mc["password"] or None,
                    identifier= mc["client_id"],
                    keepalive = mc["keepalive"],
                ) as client:
                    log.info("MQTT connected")
                    await client.subscribe(self.topics["sms_send"])
                    await client.subscribe(self.topics["call_dial"])
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._mqtt_rx(client))
                        tg.create_task(self._mqtt_tx(client))
            except aiomqtt.MqttError as e:
                log.error(f"MQTT error: {e} — retry {ri}s")
                await asyncio.sleep(ri)
            except Exception as e:
                log.error(f"MQTT fatal: {e} — retry {ri}s")
                await asyncio.sleep(ri)

    async def _mqtt_rx(self, client) -> None:
        async for msg in client.messages:
            topic = str(msg.topic)
            try: data = json.loads(msg.payload.decode())
            except: log.warning(f"Bad payload on {topic}"); continue

            if topic == self.topics["sms_send"]:
                to   = data.get("to","").strip()
                text = data.get("text","").strip()
                if to and text: await self.modem.send_sms(to, text)
                else: log.warning("sms_send: missing to/text")

            elif topic == self.topics["call_dial"]:
                to = data.get("to","").strip()
                if to: await self.modem.dial(to)

    async def _mqtt_tx(self, client) -> None:
        while True:
            topic, payload = await self._mq_q.get()
            try: await client.publish(topic, payload, qos=1)
            except Exception as e: log.error(f"MQTT publish: {e}")
            finally: self._mq_q.task_done()

    async def _pub(self, topic: str, payload: str) -> None:
        await self._mq_q.put((topic, payload))

    # ── Run ───────────────────────────────────

    async def run(self) -> None:
        self._running = True
        log.info("Gateway v1.4.0 starting")
        log.info(f"Serial {self.cfg['serial']['port']} | "
                 f"MQTT {self.mqtt_cfg['host']}:{self.mqtt_cfg['port']}")

        await self._open_serial()

        tasks = [
            asyncio.create_task(self._urc_loop(),    name="urc"),
            asyncio.create_task(self._watchdog(),    name="watchdog"),
            asyncio.create_task(self._mqtt_loop(),   name="mqtt"),
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self._cmd_loop(),    name="cmd"),
        ]
        try:
            # done_when_first_done=False — всі задачі незалежні
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            log.error(f"Gather error: {e}")
        finally:
            log.info("Cancelling all tasks...")
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        log.info("Stopping gateway...")
        self._running = False
        await self.serial.close()

# ═══════════════════════════════════════════════
# Entry
# ═══════════════════════════════════════════════

async def main() -> None:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gw.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    setup_logging(cfg["gateway"]["log_level"])

    gw = Gateway(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, lambda: asyncio.create_task(gw.stop()))
        except NotImplementedError: pass

    try:
        await gw.run()
    except KeyboardInterrupt:
        pass
    finally:
        await gw.stop()
        log.info("Gateway stopped")

if __name__ == "__main__":
    asyncio.run(main())
