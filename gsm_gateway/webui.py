"""GSM Gateway Web UI — v1.4.0  (port 8099)"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

LOG_FILE    = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"
CMD_FILE    = "/tmp/gsm_cmd.json"

HTML = r"""<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GSM Gateway</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;padding:16px}
h1{font-size:1.2rem;color:#38bdf8;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;background:DOT_CLR;box-shadow:0 0 8px DOT_CLR;display:inline-block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px;margin-bottom:16px}
.card{background:#1e293b;border-radius:8px;padding:11px 13px;border:1px solid #334155}
.lbl{font-size:.65rem;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.val{font-size:.95rem;font-weight:600}
.ok{color:#4ade80}.warn{color:#facc15}.err{color:#f87171}
.row{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.btn{padding:8px 16px;border-radius:7px;border:none;cursor:pointer;font-size:.82rem;font-weight:600;transition:opacity .15s}
.btn:hover{opacity:.82}.btn:active{opacity:.55}
.b-blue{background:#3b82f6;color:#fff}.b-red{background:#ef4444;color:#fff}.b-gray{background:#334155;color:#e2e8f0}
.form{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:16px;display:none}
.form.open{display:block}
.form input,.form textarea{width:100%;background:#0f172a;border:1px solid #334155;border-radius:5px;color:#e2e8f0;padding:7px 9px;font-size:.82rem;margin-bottom:8px;outline:none}
.form textarea{height:72px;resize:vertical;font-family:inherit}
.form input:focus,.form textarea:focus{border-color:#38bdf8}
.toast{padding:7px 12px;border-radius:6px;font-size:.8rem;font-weight:500;margin-bottom:12px;display:none}
.toast.ok{display:block;background:#14532d;color:#4ade80;border:1px solid #166534}
.toast.err{display:block;background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
.sec{font-size:.68rem;color:#475569;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.log{background:#020617;border-radius:8px;border:1px solid #1e293b;padding:10px;height:380px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.7rem;line-height:1.7}
.ll{white-space:pre-wrap;word-break:break-all}
.ll.info{color:#7dd3fc}.ll.warn{color:#fde68a}.ll.error{color:#fca5a5}.ll.debug{color:#475569}
.foot{margin-top:10px;font-size:.65rem;color:#334155;text-align:center}
</style>
</head>
<body>
<h1><span class="dot"></span> GSM MQTT Gateway v1.4.0</h1>
<div id="toast" class="toast TOAST_CLS">TOAST_MSG</div>
<div class="grid">
  <div class="card"><div class="lbl">Статус</div><div class="val ONLINE_CLS">ONLINE_TXT</div></div>
  <div class="card"><div class="lbl">Сигнал</div><div class="val SIG_CLS">SIG_TXT</div></div>
  <div class="card"><div class="lbl">Оператор</div><div class="val">OPER</div></div>
  <div class="card"><div class="lbl">Мережа</div><div class="val REG_CLS">REG_TXT</div></div>
  <div class="card"><div class="lbl">SIM</div><div class="val SIM_CLS">SIM_TXT</div></div>
  <div class="card"><div class="lbl">Оновлено</div><div class="val" style="font-size:.8rem">UPD</div></div>
</div>
<div class="row">
  <button class="btn b-blue" onclick="openSms()">✉️ Надіслати SMS</button>
  <button class="btn b-red"  onclick="doReboot()">🔄 Ребут модему</button>
  <button class="btn b-gray" onclick="location.reload()">↺ Оновити</button>
</div>
<div id="smsForm" class="form">
  <input  id="smsTo"  type="tel"  placeholder="+380..."/>
  <textarea id="smsTxt" placeholder="Текст SMS..."></textarea>
  <div class="row">
    <button class="btn b-blue" onclick="doSend()">Надіслати</button>
    <button class="btn b-gray" onclick="closeSms()">Скасувати</button>
  </div>
</div>
<div class="sec">Журнал</div>
<div class="log" id="log">LOG_HTML</div>
<div class="foot">Авто-оновлення 15с &nbsp;|&nbsp; GSM MQTT Gateway</div>
<script>
function openSms(){document.getElementById('smsForm').classList.add('open')}
function closeSms(){document.getElementById('smsForm').classList.remove('open')}
async function doReboot(){
  if(!confirm('Перезавантажити модем?'))return;
  const r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'reboot_modem'})});
  const d=await r.json();
  toast(d.ok?'Команду відправлено. Модем перезавантажується...':'Помилка: '+d.error,d.ok);
}
async function doSend(){
  const to=document.getElementById('smsTo').value.trim();
  const tx=document.getElementById('smsTxt').value.trim();
  if(!to||!tx){toast('Заповніть номер і текст',false);return;}
  const r=await fetch('/cmd',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'send_sms',to,text:tx})});
  const d=await r.json();
  if(d.ok){toast('SMS відправлено',true);document.getElementById('smsTxt').value='';closeSms();}
  else toast('Помилка: '+d.error,false);
}
function toast(msg,ok){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='toast '+(ok?'ok':'err');
  setTimeout(()=>t.className='toast',6000);
}
document.getElementById('log').scrollTop=99999;
setTimeout(()=>location.reload(),15000);
</script>
</body></html>"""


def read_status() -> dict:
    try: return json.loads(Path(STATUS_FILE).read_text(encoding="utf-8"))
    except: return {}

def read_logs(n=150) -> list[str]:
    try: return Path(LOG_FILE).read_text(encoding="utf-8").splitlines()[-n:]
    except: return []

def fmt_log(line: str) -> str:
    line = line.strip()
    if not line: return ""
    cls = "info"
    if "[WARNING]" in line or "[WARN]" in line: cls="warn"
    elif "[ERROR]" in line or "[CRITICAL]" in line: cls="error"
    elif "[DEBUG]" in line: cls="debug"
    esc = line.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f'<div class="ll {cls}">{esc}</div>'

def write_cmd(data: dict) -> bool:
    try: Path(CMD_FILE).write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8"); return True
    except: return False

def build_page(toast_cls="", toast_msg="") -> str:
    st = read_status()
    online  = st.get("online", False)
    sig_dbm = st.get("signal_dbm")
    oper    = st.get("operator") or "—"
    reg     = st.get("registration") or "—"
    sim_ok  = st.get("sim_ready", False)
    ts      = st.get("timestamp", "—")
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(ts.replace("Z","+00:00")).strftime("%H:%M:%S")
    except: pass

    dot      = "#4ade80" if online else "#ef4444"
    on_cls   = "ok" if online else "err"
    on_txt   = "Online ✓" if online else "Offline ✗"
    sig_txt  = f"{sig_dbm} dBm" if sig_dbm is not None else "—"
    sig_cls  = ("ok" if sig_dbm and sig_dbm>-85 else "warn" if sig_dbm and sig_dbm>-100 else "err") if sig_dbm else ""
    reg_map  = {"registered_home":"Домашня ✓","registered_roaming":"Роумінг",
                "not_registered":"Не зареєстровано","searching":"Пошук...","denied":"Відмовлено"}
    reg_txt  = reg_map.get(reg, reg)
    reg_cls  = "ok" if "registered" in reg else "warn"
    sim_cls  = "ok" if sim_ok else "err"
    sim_txt  = "Ready ✓" if sim_ok else "Not ready ✗"

    log_html = "\n".join(fmt_log(l) for l in read_logs() if l.strip()) or '<div class="ll debug">Лог порожній...</div>'

    return (HTML
        .replace("DOT_CLR",   dot).replace("DOT_CLR", dot)
        .replace("ONLINE_CLS",on_cls).replace("ONLINE_TXT",on_txt)
        .replace("SIG_CLS",   sig_cls).replace("SIG_TXT",sig_txt)
        .replace("OPER",      oper)
        .replace("REG_CLS",   reg_cls).replace("REG_TXT",reg_txt)
        .replace("SIM_CLS",   sim_cls).replace("SIM_TXT",sim_txt)
        .replace("UPD",       ts)
        .replace("LOG_HTML",  log_html)
        .replace("TOAST_CLS", toast_cls).replace("TOAST_MSG",toast_msg)
    )


class H(BaseHTTPRequestHandler):
    def _resp(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/status":
            self._resp(200,"application/json",json.dumps(read_status()).encode())
        elif path == "/logs":
            self._resp(200,"text/plain;charset=utf-8","\n".join(read_logs(200)).encode())
        else:
            self._resp(200,"text/html;charset=utf-8",build_page().encode())

    def do_POST(self):
        if urlparse(self.path).path != "/cmd":
            self._resp(404,"text/plain",b"Not found"); return
        n   = int(self.headers.get("Content-Length",0))
        raw = self.rfile.read(n)
        try: data = json.loads(raw.decode())
        except Exception as e:
            self._resp(400,"application/json",json.dumps({"ok":False,"error":str(e)}).encode()); return

        action = data.get("action","")
        if action == "reboot_modem":
            ok = write_cmd({"action":"reboot_modem"})
            self._resp(200,"application/json",json.dumps({"ok":ok}).encode())
        elif action == "send_sms":
            to = str(data.get("to","")).strip()
            tx = str(data.get("text","")).strip()
            if not to or not tx:
                self._resp(400,"application/json",json.dumps({"ok":False,"error":"Missing to/text"}).encode()); return
            ok = write_cmd({"action":"send_sms","to":to,"text":tx})
            self._resp(200,"application/json",json.dumps({"ok":ok}).encode())
        else:
            self._resp(400,"application/json",json.dumps({"ok":False,"error":f"Unknown: {action}"}).encode())

    def log_message(self, *a): pass


if __name__ == "__main__":
    srv = HTTPServer(("0.0.0.0", 8099), H)
    print("Web UI :8099", flush=True)
    srv.serve_forever()
