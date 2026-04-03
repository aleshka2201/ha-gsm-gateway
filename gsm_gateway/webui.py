"""
GSM Gateway Web UI
Lightweight HTTP server showing live logs and modem status.
Runs on port 8099 inside the addon container.
"""

import asyncio
import json
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading

LOG_FILE = "/tmp/gsm_gateway.log"
STATUS_FILE = "/tmp/gsm_status.json"

HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="10">
<title>GSM MQTT Gateway</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 20px;
  }
  h1 {
    font-size: 1.4rem;
    color: #4fc3f7;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  h1 span.dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    display: inline-block;
    background: {STATUS_DOT};
    box-shadow: 0 0 6px {STATUS_DOT};
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .card {
    background: #16213e;
    border-radius: 10px;
    padding: 14px 16px;
    border: 1px solid #0f3460;
  }
  .card .label {
    font-size: 0.72rem;
    color: #90a4ae;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 6px;
  }
  .card .value {
    font-size: 1.1rem;
    font-weight: 600;
    color: #e0e0e0;
  }
  .card .value.ok { color: #66bb6a; }
  .card .value.warn { color: #ffa726; }
  .card .value.err { color: #ef5350; }
  .log-box {
    background: #0d1117;
    border-radius: 10px;
    border: 1px solid #0f3460;
    padding: 14px;
    height: 420px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.7;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line.info  { color: #81d4fa; }
  .log-line.warn  { color: #ffd54f; }
  .log-line.error { color: #ef9a9a; }
  .log-line.debug { color: #78909c; }
  .section-title {
    font-size: 0.85rem;
    color: #90a4ae;
    margin-bottom: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .footer {
    margin-top: 16px;
    font-size: 0.72rem;
    color: #546e7a;
    text-align: center;
  }
</style>
</head>
<body>
<h1><span class="dot"></span> GSM MQTT Gateway</h1>

<div class="grid">
  <div class="card">
    <div class="label">Статус</div>
    <div class="value {ONLINE_CLASS}">{ONLINE_TEXT}</div>
  </div>
  <div class="card">
    <div class="label">Сигнал</div>
    <div class="value {SIGNAL_CLASS}">{SIGNAL_TEXT}</div>
  </div>
  <div class="card">
    <div class="label">Оператор</div>
    <div class="value">{OPERATOR}</div>
  </div>
  <div class="card">
    <div class="label">Реєстрація</div>
    <div class="value {REG_CLASS}">{REGISTRATION}</div>
  </div>
  <div class="card">
    <div class="label">SIM карта</div>
    <div class="value {SIM_CLASS}">{SIM_TEXT}</div>
  </div>
  <div class="card">
    <div class="label">Оновлено</div>
    <div class="value" style="font-size:0.85rem">{UPDATED}</div>
  </div>
</div>

<div class="section-title">Журнал подій</div>
<div class="log-box" id="log">
{LOG_LINES}
</div>

<div class="footer">Оновлення кожні 10 секунд &nbsp;|&nbsp; GSM MQTT Gateway v1.0.0</div>

<script>
  const log = document.getElementById('log');
  log.scrollTop = log.scrollHeight;
</script>
</body>
</html>
"""


def read_status() -> dict:
    try:
        if Path(STATUS_FILE).exists():
            with open(STATUS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def read_logs(lines: int = 120) -> list[str]:
    try:
        if Path(LOG_FILE).exists():
            with open(LOG_FILE) as f:
                all_lines = f.readlines()
                return all_lines[-lines:]
    except Exception:
        pass
    return []


def format_log_line(line: str) -> str:
    line = line.rstrip()
    if not line:
        return ""
    level = "info"
    if "[WARNING]" in line or "[WARN]" in line:
        level = "warn"
    elif "[ERROR]" in line or "[CRITICAL]" in line:
        level = "error"
    elif "[DEBUG]" in line:
        level = "debug"
    escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<div class="log-line {level}">{escaped}</div>'


def build_html() -> str:
    status = read_status()
    logs = read_logs()

    online = status.get("online", False)
    signal_dbm = status.get("signal_dbm")
    operator = status.get("operator") or "—"
    registration = status.get("registration") or "—"
    sim_ready = status.get("sim_ready", False)
    updated = status.get("timestamp", "—")
    if updated != "—":
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            updated = dt.strftime("%H:%M:%S")
        except Exception:
            pass

    online_class = "ok" if online else "err"
    online_text = "Online ✓" if online else "Offline ✗"
    status_dot = "#66bb6a" if online else "#ef5350"

    if signal_dbm is not None:
        signal_text = f"{signal_dbm} dBm"
        signal_class = "ok" if signal_dbm > -85 else ("warn" if signal_dbm > -100 else "err")
    else:
        signal_text = "—"
        signal_class = ""

    reg_class = "ok" if "registered" in registration else "warn"
    sim_class = "ok" if sim_ready else "err"
    sim_text = "Ready ✓" if sim_ready else "Not ready ✗"

    reg_map = {
        "registered_home": "Домашня мережа",
        "registered_roaming": "Роумінг",
        "not_registered": "Не зареєстровано",
        "searching": "Пошук мережі",
        "denied": "Відмовлено",
    }
    registration_text = reg_map.get(registration, registration)

    log_html = "\n".join(format_log_line(l) for l in logs if l.strip())
    if not log_html:
        log_html = '<div class="log-line debug">Лог порожній. Зачекайте запуску шлюзу...</div>'

    return (
        HTML
        .replace("{STATUS_DOT}", status_dot)
        .replace("{ONLINE_CLASS}", online_class)
        .replace("{ONLINE_TEXT}", online_text)
        .replace("{SIGNAL_CLASS}", signal_class)
        .replace("{SIGNAL_TEXT}", signal_text)
        .replace("{OPERATOR}", operator)
        .replace("{REG_CLASS}", reg_class)
        .replace("{REGISTRATION}", registration_text)
        .replace("{SIM_CLASS}", sim_class)
        .replace("{SIM_TEXT}", sim_text)
        .replace("{UPDATED}", updated)
        .replace("{LOG_LINES}", log_html)
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        html = build_html()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode())))
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8099), Handler)
    print("Web UI listening on :8099")
    server.serve_forever()
