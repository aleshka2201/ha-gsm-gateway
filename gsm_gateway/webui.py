"""
GSM Gateway Web UI — v1.2.0
HTTP server on port 8099.
GET  /          — dashboard (auto-refresh)
POST /cmd       — JSON command: {"action": "reboot_modem"} or {"action": "send_sms", "to": "...", "text": "..."}
GET  /status    — raw JSON status
GET  /logs      — raw log text
"""

import json
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LOG_FILE    = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"
CMD_FILE    = "/tmp/gsm_cmd.json"

# ─────────────────────────────────────────────
# HTML template
# ─────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GSM Gateway</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding:16px}
h1{font-size:1.25rem;color:#38bdf8;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:__DOT__;box-shadow:0 0 8px __DOT__}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}
.card{background:#1e293b;border-radius:10px;padding:12px 14px;border:1px solid #334155}
.card .lbl{font-size:.68rem;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.card .val{font-size:1rem;font-weight:600}
.ok{color:#4ade80}.warn{color:#facc15}.err{color:#f87171}
.section{font-size:.75rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;margin-top:20px}

/* Buttons */
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:20px}
.btn{padding:9px 18px;border-radius:8px;border:none;cursor:pointer;font-size:.85rem;font-weight:600;transition:opacity .15s}
.btn:hover{opacity:.85}
.btn:active{opacity:.65}
.btn-red{background:#ef4444;color:#fff}
.btn-blue{background:#3b82f6;color:#fff}
.btn-gray{background:#334155;color:#e2e8f0}

/* SMS form */
.sms-form{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:20px;display:none}
.sms-form.open{display:block}
.sms-form input,.sms-form textarea{width:100%;background:#0f172a;border:1px solid #334155;border-radius:6px;color:#e2e8f0;padding:8px 10px;font-size:.85rem;margin-bottom:10px;outline:none}
.sms-form textarea{height:80px;resize:vertical;font-family:inherit}
.sms-form input:focus,.sms-form textarea:focus{border-color:#38bdf8}
.toast{display:none;padding:8px 14px;border-radius:7px;font-size:.82rem;margin-bottom:14px;font-weight:500}
.toast.ok{display:block;background:#14532d;color:#4ade80;border:1px solid #166534}
.toast.err{display:block;background:#450a0a;color:#f87171;border:1px solid #7f1d1d}

/* Log */
.log-box{background:#020617;border-radius:10px;border:1px solid #1e293b;padding:12px;height:400px;overflow-y:auto;font-family:'JetBrains Mono','Fira Code',monospace;font-size:.73rem;line-height:1.75}
.ll{white-space:pre-wrap;word-break:break-all}
.ll.info{color:#7dd3fc}.ll.warn{color:#fde68a}.ll.error{color:#fca5a5}.ll.debug{color:#475569}
.footer{margin-top:14px;font-size:.68rem;color:#334155;text-align:center}
.auto-badge{font-size:.65rem;color:#475569;margin-left:8px}
</style>
</head>
<body>
<h1><span class="dot"></span> GSM MQTT Gateway <span class="auto-badge">auto-refresh 15s</span></h1>

<div id="toast" class="toast __TOAST_CLASS__">__TOAST_MSG__</div>

<div class="grid">
  <div class="card"><div class="lbl">Статус</div><div class="val __ONLINE_CLS__">__ONLINE__</div></div>
  <div class="card"><div class="lbl">Сигнал</div><div class="val __SIG_CLS__">__SIGNAL__</div></div>
  <div class="card"><div class="lbl">Оператор</div><div class="val">__OPER__</div></div>
  <div class="card"><div class="lbl">Мережа</div><div class="val __REG_CLS__">__REG__</div></div>
  <div class="card"><div class="lbl">SIM</div><div class="val __SIM_CLS__">__SIM__</div></div>
  <div class="card"><div class="lbl">Оновлено</div><div class="val" style="font-size:.82rem">__UPD__</div></div>
</div>

<div class="section">Дії</div>
<div class="actions">
  <button class="btn btn-blue" onclick="toggleSmsForm()">✉️ Надіслати SMS</button>
  <button class="btn btn-red"  onclick="doReboot()">🔄 Ребут модему</button>
  <button class="btn btn-gray" onclick="location.reload()">↺ Оновити</button>
</div>

<div id="smsForm" class="sms-form">
  <input  id="smsTo"   type="tel"  placeholder="Номер телефону (+380...)" />
  <textarea id="smsTxt" placeholder="Текст повідомлення..."></textarea>
  <div style="display:flex;gap:8px">
    <button class="btn btn-blue" onclick="doSendSms()">Надіслати</button>
    <button class="btn btn-gray" onclick="toggleSmsForm()">Скасувати</button>
  </div>
</div>

<div class="section">Журнал подій</div>
<div class="log-box" id="log">__LOG__</div>
<div class="footer">GSM MQTT Gateway v1.2.0</div>

<script>
function toggleSmsForm(){
  var f=document.getElementById('smsForm');
  f.classList.toggle('open');
}

async function doReboot(){
  if(!confirm('Перезавантажити модем?')) return;
  try{
    var r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'reboot_modem'})});
    var d=await r.json();
    showToast(d.ok?'Команду відправлено. Модем перезавантажується...':'Помилка: '+d.error, d.ok);
  }catch(e){showToast('Помилка зв\'язку: '+e.message,false);}
}

async function doSendSms(){
  var to=document.getElementById('smsTo').value.trim();
  var text=document.getElementById('smsTxt').value.trim();
  if(!to||!text){showToast('Заповніть номер і текст',false);return;}
  try{
    var r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'send_sms',to:to,text:text})});
    var d=await r.json();
    if(d.ok){
      showToast('SMS поставлено в чергу відправки',true);
      document.getElementById('smsTxt').value='';
      document.getElementById('smsForm').classList.remove('open');
    }else{showToast('Помилка: '+d.error,false);}
  }catch(e){showToast('Помилка зв\'язку: '+e.message,false);}
}

function showToast(msg,ok){
  var t=document.getElementById('toast');
  t.textContent=msg;
  t.className='toast '+(ok?'ok':'err');
  setTimeout(()=>{t.className='toast';},5000);
}

// Auto-scroll log
var log=document.getElementById('log');
if(log) log.scrollTop=log.scrollHeight;

// Auto-refresh
setTimeout(()=>location.reload(), 15000);
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def read_status() -> dict:
    try:
        if Path(STATUS_FILE).exists():
            return json.loads(Path(STATUS_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def read_logs(n: int = 150) -> list[str]:
    try:
        if Path(LOG_FILE).exists():
            lines = Path(LOG_FILE).read_text(encoding="utf-8").splitlines()
            return lines[-n:]
    except Exception:
        pass
    return []


def fmt_log(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    level = "info"
    if "[WARNING]" in line or "[WARN]" in line:
        level = "warn"
    elif "[ERROR]" in line or "[CRITICAL]" in line:
        level = "error"
    elif "[DEBUG]" in line:
        level = "debug"
    esc = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<div class="ll {level}">{esc}</div>'


def write_cmd(data: dict) -> bool:
    try:
        Path(CMD_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def build_page(toast_cls: str = "", toast_msg: str = "") -> str:
    st = read_status()
    logs = read_logs()

    online    = st.get("online", False)
    sig_dbm   = st.get("signal_dbm")
    operator  = st.get("operator") or "—"
    reg       = st.get("registration") or "—"
    sim_ready = st.get("sim_ready", False)
    ts        = st.get("timestamp", "—")

    if ts != "—":
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = dt.strftime("%H:%M:%S")
        except Exception:
            pass

    dot       = "#4ade80" if online else "#ef4444"
    on_cls    = "ok" if online else "err"
    on_txt    = "Online ✓" if online else "Offline ✗"

    if sig_dbm is not None:
        sig_txt = f"{sig_dbm} dBm"
        sig_cls = "ok" if sig_dbm > -85 else ("warn" if sig_dbm > -100 else "err")
    else:
        sig_txt, sig_cls = "—", ""

    reg_map = {
        "registered_home":    "Домашня ✓",
        "registered_roaming": "Роумінг",
        "not_registered":     "Не зареєстровано",
        "searching":          "Пошук...",
        "denied":             "Відмовлено",
    }
    reg_txt = reg_map.get(reg, reg)
    reg_cls = "ok" if "registered" in reg else "warn"
    sim_cls = "ok" if sim_ready else "err"
    sim_txt = "Ready ✓" if sim_ready else "Not ready ✗"

    log_html = "\n".join(fmt_log(l) for l in logs if l.strip())
    if not log_html:
        log_html = '<div class="ll debug">Лог порожній. Зачекайте запуску...</div>'

    return (HTML
        .replace("__DOT__",       dot)
        .replace("__ONLINE_CLS__", on_cls)
        .replace("__ONLINE__",    on_txt)
        .replace("__SIG_CLS__",   sig_cls)
        .replace("__SIGNAL__",    sig_txt)
        .replace("__OPER__",      operator)
        .replace("__REG_CLS__",   reg_cls)
        .replace("__REG__",       reg_txt)
        .replace("__SIM_CLS__",   sim_cls)
        .replace("__SIM__",       sim_txt)
        .replace("__UPD__",       ts)
        .replace("__LOG__",       log_html)
        .replace("__TOAST_CLASS__", toast_cls)
        .replace("__TOAST_MSG__",   toast_msg)
    )


# ─────────────────────────────────────────────
# HTTP handler
# ─────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/status":
            st = read_status()
            self._send(200, "application/json", json.dumps(st).encode())

        elif path == "/logs":
            logs = "\n".join(read_logs(200))
            self._send(200, "text/plain; charset=utf-8", logs.encode("utf-8"))

        else:
            html = build_page()
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/cmd":
            length = int(self.headers.get("Content-Length", 0))
            raw    = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                resp = json.dumps({"ok": False, "error": f"JSON parse error: {e}"}).encode()
                self._send(400, "application/json", resp)
                return

            action = data.get("action", "")

            if action == "reboot_modem":
                ok = write_cmd({"action": "reboot_modem"})
                resp = json.dumps({"ok": ok}).encode()
                self._send(200, "application/json", resp)

            elif action == "send_sms":
                to   = str(data.get("to", "")).strip()
                text = str(data.get("text", "")).strip()
                if not to or not text:
                    resp = json.dumps({"ok": False, "error": "Missing 'to' or 'text'"}).encode()
                    self._send(400, "application/json", resp)
                    return
                ok = write_cmd({"action": "send_sms", "to": to, "text": text})
                resp = json.dumps({"ok": ok}).encode()
                self._send(200, "application/json", resp)

            else:
                resp = json.dumps({"ok": False, "error": f"Unknown action: {action}"}).encode()
                self._send(400, "application/json", resp)
        else:
            self._send(404, "text/plain", b"Not found")

    def log_message(self, fmt, *args):
        pass  # відключаємо access log


# ─────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8099), Handler)
    print("Web UI listening on :8099", flush=True)
    server.serve_forever()
