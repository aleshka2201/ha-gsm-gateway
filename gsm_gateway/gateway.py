"""
GSM-MQTT Gateway v1.6.0
=======================
Нова архітектура: SerialWorker — єдиний task що має доступ до serial.
Всі операції відправляються через Job queue, результат через Future.
Ніякої конкуренції за serial. Зависання неможливе — кожен job має таймаут.
"""

import asyncio
import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import aiomqtt
import serial_asyncio
import yaml

LOG_FILE    = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"
CMD_FILE    = "/tmp/gsm_cmd.json"

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

def ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def norm(raw: str) -> str:
    s = str(raw).strip().strip("'\"")
    s = re.sub(r"[\s\(\)\-]", "", s)
    s = re.sub(r"[^\d+]", "", s)
    return ("+" + s.replace("+", "")) if "+" in s else s

def phones_eq(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if na == nb: return True
    if na.lstrip("+") == nb.lstrip("+"): return True
    da, db = re.sub(r"\D","",na), re.sub(r"\D","",nb)
    return len(da) >= 9 and len(db) >= 9 and da[-9:] == db[-9:]

def is_trusted(phone: str, lst: list) -> bool:
    return any(phones_eq(phone, t) for t in lst)

def parse_trusted(raw) -> list:
    if not raw: return []
    items = raw if isinstance(raw, list) else [raw]
    return [n for n in (norm(str(i)) for i in items) if n]

def ucs2_enc(t: str) -> str:
    return t.encode("utf-16-be").hex().upper()

def ucs2_dec(h: str) -> str:
    try:    return bytes.fromhex(h).decode("utf-16-be")
    except: return h

def build_pdu(phone: str, text: str) -> tuple[str, int]:
    d = re.sub(r"\D", "", phone)
    toa = "91" if phone.startswith("+") else "81"
    p = d if len(d) % 2 == 0 else d + "F"
    pe = "".join(p[i+1]+p[i] for i in range(0, len(p), 2))
    pl = hex(len(d))[2:].upper().zfill(2)
    te = ucs2_enc(text)
    ul = hex(len(text)*2)[2:].upper().zfill(2)
    pdu = "00"+"11"+"00"+pl+toa+pe+"00"+"08"+"AA"+ul+te
    return pdu, len(pdu)//2-1

def decode_pdu(pdu: str) -> tuple[str, str]:
    pos = 0
    def rd(n):
        nonlocal pos; v = pdu[pos:pos+n]; pos += n; return v
    sl = int(rd(2),16); rd(sl*2)
    pt = int(rd(2),16)
    al = int(rd(2),16); at = int(rd(2),16)
    ar = rd((al+1)//2*2)
    if at in (0x91,0x81):
        ph = ""
        for i in range(0, len(ar)-1, 2):
            ph += ar[i+1]
            if ar[i] != "F": ph += ar[i]
        if at == 0x91: ph = "+" + ph
    else: ph = ar
    rd(2); dcs = int(rd(2),16)
    vf = (pt>>3)&3
    if vf == 2: rd(2)
    elif vf in (1,3): rd(14)
    rd(14); udl = int(rd(2),16)
    if dcs & 0x08:         txt = ucs2_dec(pdu[pos:])
    elif (dcs>>2)&3 == 1:  txt = bytes.fromhex(pdu[pos:]).decode("latin-1","replace")
    else:                  txt = _gsm7(pdu[pos:], udl)
    return ph, txt

def _gsm7(h: str, n: int) -> str:
    T = ("@\u00a3$\u00a5\u00e8\u00e9\u00f9\u00ec\u00f2\u00c7\n\u00d8\u00f8\r\u00c5\u00e5"
         "\u0394_\u03a6\u0393\u039b\u03a9\u03a0\u03a8\u03a3\u03b8\u039e\x1b\u00c6\u00e6\u00df\u00c9"
         " !\"#\u00a4%&'()*+,-./0123456789:;<=>?"
         "\u00a1ABCDEFGHIJKLMNOPQRSTUVWXYZ\u00c4\u00d6\u00d1\u00dc\u00a7"
         "\u00bfabcdefghijklmnopqrstuvwxyz\u00e4\u00f6\u00f1\u00fc\u00e0")
    try:
        b = int.from_bytes(bytes.fromhex(h), "little")
        return "".join(T[(b>>(i*7))&0x7f] if (b>>(i*7))&0x7f<len(T) else "?" for i in range(n))
    except: return h

@dataclass
class Job:
    kind:    str
    payload: dict = field(default_factory=dict)
    future:  Any  = field(default=None)

class SerialWorker:
    """Єдиний task з доступом до serial. Всі операції — через Job queue."""

    FINAL = {"OK","ERROR","NO CARRIER","BUSY","NO ANSWER","NO DIALTONE"}

    def __init__(self, port: str, baud: int, at_timeout: float):
        self.port       = port
        self.baud       = baud
        self.at_timeout = at_timeout
        self._q:   asyncio.Queue = asyncio.Queue()
        self._r    = None
        self._w    = None
        self._alive = False
        self._last_rx = time.monotonic()
        self._task: asyncio.Task | None = None
        self.on_urc = None

    @property
    def alive(self) -> bool: return self._alive

    @property
    def idle_sec(self) -> float: return time.monotonic() - self._last_rx

    async def submit(self, job: Job, timeout: float = 15.0) -> Any:
        loop = asyncio.get_running_loop()
        job.future = loop.create_future()
        await self._q.put(job)
        try:
            return await asyncio.wait_for(asyncio.shield(job.future), timeout=timeout)
        except asyncio.TimeoutError:
            if not job.future.done(): job.future.cancel()
            raise

    async def send_at(self, cmd: str, wait: str = "OK", timeout: float | None = None) -> str:
        t = timeout or self.at_timeout
        result = await self.submit(Job("at", {"cmd":cmd,"wait":wait,"timeout":t}), timeout=t+2)
        if isinstance(result, Exception): raise result
        return result

    async def send_sms(self, pdu_len: int, pdu: str) -> bool:
        result = await self.submit(Job("sms", {"pdu_len":pdu_len,"pdu":pdu}), timeout=45)
        if isinstance(result, Exception): raise result
        return bool(result)

    async def ping(self) -> bool:
        try:
            r = await self.send_at("AT", wait="OK", timeout=3.0)
            return "OK" in r
        except Exception: return False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="serial_worker")

    async def stop(self) -> None:
        self._alive = False
        try: self._q.put_nowait(Job("stop"))
        except: pass
        if self._task:
            try: await asyncio.wait_for(self._task, timeout=5)
            except Exception: self._task.cancel()
            self._task = None
        await self._close_port()

    async def _open_port(self) -> bool:
        try:
            self._r, self._w = await asyncio.wait_for(
                serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baud),
                timeout=10
            )
            self._alive = True
            self._last_rx = time.monotonic()
            log.info(f"Serial opened: {self.port}")
            return True
        except Exception as e:
            log.error(f"Serial open failed: {e}")
            return False

    async def _close_port(self) -> None:
        self._alive = False
        if self._w:
            try: self._w.close(); await self._w.wait_closed()
            except: pass
        self._r = self._w = None

    async def _readline(self, timeout: float) -> str:
        try:
            raw = await asyncio.wait_for(self._r.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return ""
        line = raw.decode(errors="replace").rstrip("\r\n")
        if line.strip():
            self._last_rx = time.monotonic()
            log.debug(f"<< {line!r}")
        return line

    async def _write(self, data: bytes) -> None:
        self._w.write(data)
        await self._w.drain()

    async def _do_at(self, cmd: str, wait: str, timeout: float) -> str:
        log.debug(f">> {cmd!r}")
        await self._write(f"{cmd}\r\n".encode())
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0: raise asyncio.TimeoutError(f"AT timeout: {cmd!r}")
            line = await self._readline(min(rem, 2.0))
            if not line.strip(): continue
            lines.append(line)
            if line in (wait, *self.FINAL): break
            if line.startswith(("+CMS ERROR","+CME ERROR")): break
        return "\n".join(lines)

    async def _do_sms(self, pdu_len: int, pdu: str) -> bool:
        log.debug(f">> AT+CMGS={pdu_len}")
        await self._write(f"AT+CMGS={pdu_len}\r\n".encode())
        # Wait for '>'
        deadline = time.monotonic() + 10
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0: log.error("Timeout waiting for '>'"); return False
            line = await self._readline(min(rem, 2.0))
            log.debug(f"sms1<< {line!r}")
            if ">" in line: break
            if line.strip() in ("ERROR",) or "CMS ERROR" in line:
                log.error(f"CMGS rejected: {line!r}"); return False
        # Send PDU
        log.debug(f">> [PDU {len(pdu)}] ^Z")
        await self._write((pdu + "\x1A").encode())
        lines: list[str] = []
        deadline = time.monotonic() + 30
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0: log.error("Timeout waiting for SMS confirm"); return False
            line = await self._readline(min(rem, 2.0))
            log.debug(f"sms2<< {line!r}")
            if not line.strip(): continue
            lines.append(line)
            if line in ("OK","ERROR") or line.startswith(("+CMS ERROR","+CMGS")): break
        ok = "OK" in lines or any(l.startswith("+CMGS") for l in lines)
        log.info(f"SMS {'OK' if ok else 'FAILED'}: {lines}")
        return ok

    async def _run(self) -> None:
        while not await self._open_port():
            log.warning("Retry open in 3s...")
            await asyncio.sleep(3)

        while self._alive:
            try:
                job = self._q.get_nowait()
            except asyncio.QueueEmpty:
                await self._try_read_urc()
                continue

            if job.kind == "stop": break

            try:
                if   job.kind == "at":  result = await self._do_at(job.payload["cmd"], job.payload["wait"], job.payload["timeout"])
                elif job.kind == "sms": result = await self._do_sms(job.payload["pdu_len"], job.payload["pdu"])
                else:                   result = None
                if job.future and not job.future.done(): job.future.set_result(result)
            except Exception as e:
                log.error(f"Job {job.kind} error: {e}")
                if job.future and not job.future.done(): job.future.set_result(e)

        await self._close_port()

    async def _try_read_urc(self) -> None:
        if not self._r: return
        try:
            raw = await asyncio.wait_for(self._r.readline(), timeout=0.1)
            decoded = raw.decode(errors="replace").rstrip("\r\n")
            if decoded.strip():
                self._last_rx = time.monotonic()
                log.debug(f"<< {decoded!r}")
                if self.on_urc:
                    asyncio.create_task(self.on_urc(decoded))
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            if self._alive: log.error(f"URC error: {e}")

class Modem:
    def __init__(self, w: SerialWorker):
        self.w = w

    async def init(self) -> None:
        for cmd, exp in [
            ("AT","OK"),("ATE0","OK"),("AT+CMGF=0","OK"),
            ("AT+CNMI=2,2,0,0,0","OK"),("AT+CLIP=1","OK"),
            ("AT+CLTS=1","OK"),('AT+CSCS="UCS2"',"OK"),
        ]:
            try:    await self.w.send_at(cmd, wait=exp)
            except Exception as e: log.warning(f"Init {cmd!r}: {e}")
        log.info("Modem initialized")

    async def wait_ready(self, attempts: int = 15, delay: float = 2.0) -> bool:
        log.info(f"Waiting for modem ({attempts} attempts x {delay}s)...")
        for i in range(attempts):
            if await self.w.ping():
                log.info(f"Modem ready (attempt {i+1})"); return True
            log.debug(f"Ping {i+1}/{attempts}")
            await asyncio.sleep(delay)
        log.error("Modem not responding"); return False

    async def status(self) -> dict:
        s: dict = {"timestamp":ts(),"online":False,"signal_rssi":None,
                   "signal_dbm":None,"operator":None,"sim_ready":False,"registration":None}
        try:
            r = await self.w.send_at("AT+CSQ")
            m = re.search(r"\+CSQ:\s*(\d+),", r)
            if m:
                rssi = int(m.group(1)); s["signal_rssi"] = rssi
                if rssi not in (0,99): s["signal_dbm"] = -113+rssi*2; s["online"] = True
            r = await self.w.send_at("AT+CPIN?"); s["sim_ready"] = "READY" in r
            r = await self.w.send_at("AT+CREG?")
            m = re.search(r"\+CREG:\s*\d+,(\d+)", r)
            if m: s["registration"] = {"0":"not_registered","1":"registered_home","2":"searching","3":"denied","5":"registered_roaming"}.get(m.group(1), m.group(1))
            r = await self.w.send_at("AT+COPS?")
            m = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', r)
            if m:
                try: s["operator"] = ucs2_dec(m.group(1))
                except: s["operator"] = m.group(1)
        except Exception as e: log.warning(f"Status: {e}")
        try: Path(STATUS_FILE).write_text(json.dumps(s,ensure_ascii=False),encoding="utf-8")
        except: pass
        return s

    async def send_sms(self, phone: str, text: str) -> bool:
        pdu, pdu_len = build_pdu(phone, text)
        log.info(f"Sending SMS → {phone} (pdu_len={pdu_len})")
        try:    return await self.w.send_sms(pdu_len, pdu)
        except Exception as e: log.error(f"send_sms: {e}"); return False

    async def hangup(self) -> None:
        try: await self.w.send_at("ATH", timeout=3.0)
        except: pass

    async def dial(self, phone: str) -> bool:
        try:
            r = await self.w.send_at(f"ATD{phone};", wait="OK", timeout=15)
            return "OK" in r
        except Exception as e: log.error(f"Dial: {e}"); return False

    async def soft_reboot(self) -> None:
        log.info("AT+CFUN=1,1")
        try: await asyncio.wait_for(self.w.send_at("AT+CFUN=1,1", wait="OK", timeout=3), timeout=4)
        except: pass

def parse_urc(line: str) -> dict | None:
    if line.startswith("+CMT:"): return {"t":"sms_hdr","raw":line}
    m = re.match(r'\+CLIP:\s*"([^"]*)"', line)
    if m:
        c = m.group(1)
        try: c = ucs2_dec(c)
        except: pass
        return {"t":"call","caller":c}
    if line == "RING": return {"t":"ring"}
    if line in ("NO CARRIER","BUSY","NO ANSWER"): return {"t":"hangup","r":line}
    return None

class Gateway:
    def __init__(self, cfg: dict):
        self.cfg = cfg; self.topics = cfg["topics"]
        self.gw_cfg = cfg["gateway"]; self.mqtt_cfg = cfg["mqtt"]
        self.worker = SerialWorker(cfg["serial"]["port"], cfg["serial"]["baudrate"], cfg["gateway"]["at_command_timeout"])
        self.modem = Modem(self.worker)
        self._mq_q: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._trusted = parse_trusted(self.gw_cfg.get("trusted_numbers",[]))
        log.info(f"Trusted: {self._trusted}")
        self._sms_hdr: str | None = None
        self.worker.on_urc = self._on_urc_line

    async def _start_worker(self) -> None:
        self.worker.start()
        if await self.modem.wait_ready():
            await self.modem.init()
            asyncio.create_task(self._pub_status_now(), name="init_status")
        else:
            log.error("Modem not ready — watchdog will retry")

    async def _restart_worker(self, wait_secs: float = 0) -> None:
        log.info(f"Restarting worker (wait={wait_secs}s)...")
        await self.worker.stop()
        if wait_secs > 0: await asyncio.sleep(wait_secs)
        await self._start_worker()

    async def _pub_status_now(self) -> None:
        await asyncio.sleep(1)
        try:
            s = await self.modem.status()
            await self._pub(self.topics["status"], json.dumps(s, ensure_ascii=False))
            log.info(f"Init status: online={s['online']} signal={s.get('signal_dbm')}dBm op={s.get('operator')}")
        except Exception as e: log.warning(f"Init status: {e}")

    async def _watchdog(self) -> None:
        wd = self.cfg["serial"]["watchdog_timeout"]
        while self._running:
            await asyncio.sleep(15)
            if not self.worker.alive:
                log.warning("Watchdog: worker dead — restarting")
                await self._restart_worker(); continue
            if self.worker.idle_sec > wd:
                log.warning(f"Watchdog: idle {self.worker.idle_sec:.0f}s — restarting")
                await self._restart_worker()

    async def _on_urc_line(self, line: str) -> None:
        try:
            if self._sms_hdr is not None:
                await self._on_sms(self._sms_hdr, line); self._sms_hdr = None; return
            ev = parse_urc(line)
            if not ev: return
            if ev["t"] == "sms_hdr": self._sms_hdr = ev["raw"]
            elif ev["t"] == "call":
                await self._on_call(ev["caller"]); await asyncio.sleep(0.3); await self.modem.hangup()
            elif ev["t"] == "ring": log.debug("RING")
            elif ev["t"] == "hangup": log.debug(f"Call ended: {ev['r']}")
        except Exception as e: log.error(f"URC handler: {e}")

    async def _on_sms(self, hdr: str, pdu: str) -> None:
        try:
            phone, text = decode_pdu(pdu)
            trusted = 1 if is_trusted(phone, self._trusted) else 0
            log.info(f"SMS from {phone} trusted={trusted}: {text[:60]}")
            await self._pub(self.topics["sms_inbox"], json.dumps({"from":phone,"text":text,"trusted":trusted,"timestamp":ts()},ensure_ascii=False))
        except Exception as e: log.error(f"SMS decode: {e} | pdu={pdu!r}")

    async def _on_call(self, caller: str) -> None:
        trusted = 1 if is_trusted(caller, self._trusted) else 0
        log.info(f"Call from {caller} trusted={trusted}")
        await self._pub(self.topics["call_inbox"], json.dumps({"from":caller,"action":"hangup","trusted":trusted,"timestamp":ts()},ensure_ascii=False))

    async def _status_loop(self) -> None:
        interval = self.gw_cfg["status_interval"]
        while self._running:
            await asyncio.sleep(interval)
            if not self.worker.alive: continue
            try:
                s = await self.modem.status()
                await self._pub(self.topics["status"], json.dumps(s, ensure_ascii=False))
                log.info(f"Status: online={s['online']} signal={s.get('signal_dbm')}dBm op={s.get('operator')} reg={s.get('registration')}")
            except Exception as e: log.error(f"Status loop: {e}")

    async def _cmd_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1)
            p = Path(CMD_FILE)
            if not p.exists(): continue
            try:
                raw = p.read_text(encoding="utf-8").strip(); p.unlink(missing_ok=True)
                if not raw: continue
                cmd = json.loads(raw)
            except Exception as e: log.warning(f"CMD: {e}"); continue
            action = cmd.get("action","")
            log.info(f"WebUI cmd: {action}")
            if action == "reboot_modem":
                log.info("=== MODEM REBOOT ===")
                if self.worker.alive: await self.modem.soft_reboot()
                await self._restart_worker(wait_secs=12)
            elif action == "send_sms":
                to = cmd.get("to","").strip(); text = cmd.get("text","").strip()
                if to and text: await self.modem.send_sms(to, text)
                else: log.warning("send_sms: missing to/text")

    async def _mqtt_loop(self) -> None:
        mc = self.mqtt_cfg; ri = mc.get("reconnect_interval",5)
        while self._running:
            try:
                log.info(f"MQTT -> {mc['host']}:{mc['port']}")
                async with aiomqtt.Client(hostname=mc["host"],port=mc["port"],username=mc["username"] or None,password=mc["password"] or None,identifier=mc["client_id"],keepalive=mc["keepalive"]) as client:
                    log.info("MQTT connected")
                    await client.subscribe(self.topics["sms_send"]); await client.subscribe(self.topics["call_dial"])
                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._mqtt_rx(client)); tg.create_task(self._mqtt_tx(client))
            except aiomqtt.MqttError as e: log.error(f"MQTT: {e} retry {ri}s"); await asyncio.sleep(ri)
            except Exception as e: log.error(f"MQTT fatal: {e} retry {ri}s"); await asyncio.sleep(ri)

    async def _mqtt_rx(self, client) -> None:
        async for msg in client.messages:
            topic = str(msg.topic)
            try: data = json.loads(msg.payload.decode())
            except: log.warning(f"Bad payload: {topic}"); continue
            if topic == self.topics["sms_send"]:
                to = data.get("to","").strip(); tx = data.get("text","").strip()
                if to and tx: await self.modem.send_sms(to, tx)
            elif topic == self.topics["call_dial"]:
                to = data.get("to","").strip()
                if to: await self.modem.dial(to)

    async def _mqtt_tx(self, client) -> None:
        while True:
            topic, payload = await self._mq_q.get()
            try: await client.publish(topic, payload, qos=1)
            except Exception as e: log.error(f"MQTT pub: {e}")
            finally: self._mq_q.task_done()

    async def _pub(self, topic: str, payload: str) -> None:
        await self._mq_q.put((topic, payload))

    async def run(self) -> None:
        self._running = True
        log.info("Gateway v1.6.0 starting")
        await self._start_worker()
        tasks = [
            asyncio.create_task(self._watchdog(),    name="watchdog"),
            asyncio.create_task(self._mqtt_loop(),   name="mqtt"),
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self._cmd_loop(),    name="cmd"),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, r in zip(["watchdog","mqtt","status","cmd"], results):
            if isinstance(r, Exception): log.error(f"Task {name!r}: {r}")

    async def stop(self) -> None:
        log.info("Stopping..."); self._running = False
        await self.worker.stop()

async def main() -> None:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gw.yaml"
    with open(cfg_path, encoding="utf-8") as f: cfg = yaml.safe_load(f)
    setup_logging(cfg["gateway"]["log_level"])
    gw = Gateway(cfg)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, lambda: asyncio.create_task(gw.stop()))
        except NotImplementedError: pass
    try: await gw.run()
    except KeyboardInterrupt: pass
    finally: await gw.stop(); log.info("Stopped")

if __name__ == "__main__":
    asyncio.run(main())
