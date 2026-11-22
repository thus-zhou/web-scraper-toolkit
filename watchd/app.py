#!/usr/bin/env python3
"""watchd — 轻量网页监控 + 变更事件服务（单文件，stdlib + bs4）。

跑在 Altare 芬兰沙盒上：SQLite 存状态，后台线程按间隔抓取，变更写事件并可 POST 到 webhook。
HTTP: 8790
  GET  /health
  GET  /                     简易面板（30s 自动刷新）
  GET  /api/monitors
  POST /api/monitors         {"name":..,"url":..,"selector":..,"interval_s":300}
  DELETE /api/monitors/<id>
  GET  /api/events?limit=50
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

DB = os.environ.get("WATCHD_DB", "/root/watchd/watchd.db")
PORT = int(os.environ.get("WATCHD_PORT", "8790"))
CONFIG = os.environ.get("WATCHD_CONFIG", "/root/watchd/config.json")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36"

_lock = threading.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                selector TEXT NOT NULL,
                interval_s INTEGER NOT NULL DEFAULT 300,
                last_value TEXT,
                last_check REAL,
                last_status TEXT DEFAULT 'new',
                created REAL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
            """
        )


def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def fetch_text(url: str, selector: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,*;q=0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(3_000_000)
    soup = BeautifulSoup(raw, "html.parser")
    node = soup.select_one(selector)
    if node is None:
        raise LookupError(f"selector {selector!r} matched nothing")
    text = " ".join(node.get_text(" ", strip=True).split())
    return text[:500]


def notify(event: dict, hook: str) -> None:
    if not hook:
        return
    body = json.dumps(event, ensure_ascii=False).encode()
    req = urllib.request.Request(hook, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as exc:  # 通知失败不影响监控本身
        print(f"[watchd] webhook failed: {exc}", flush=True)


def check_one(row: sqlite3.Row, hook: str) -> None:
    mid, name, url, selector = row["id"], row["name"], row["url"], row["selector"]
    old = row["last_value"]
    status, new = "ok", None
    try:
        new = fetch_text(url, selector)
    except LookupError:
        status = "missing"
    except Exception as exc:
        status = f"error: {type(exc).__name__}"
        print(f"[watchd] {name}: {exc}", flush=True)

    with db() as conn:
        if status == "ok" and new != old:
            kind = "new" if old is None else "change"
            conn.execute(
                "INSERT INTO events(monitor_id, ts, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                (mid, time.time(), kind, old, new),
            )
        elif status != "ok" and row["last_status"] != status:
            conn.execute(
                "INSERT INTO events(monitor_id, ts, kind, old_value, new_value) VALUES (?,?,?,?,?)",
                (mid, time.time(), "alert", row["last_status"], status),
            )
        conn.execute(
            "UPDATE monitors SET last_value=?, last_check=?, last_status=? WHERE id=?",
            (new if status == "ok" else old, time.time(), status, mid),
        )
    if status == "ok" and new != old:
        notify({"monitor": name, "url": url, "old": old, "new": new}, hook)


def scheduler() -> None:
    while True:
        try:
            hook = str(load_config().get("webhook_url") or "")
            with db() as conn:
                due = conn.execute(
                    "SELECT * FROM monitors WHERE last_check IS NULL OR last_check + interval_s <= ?",
                    (time.time(),),
                ).fetchall()
            for row in due:
                with _lock:
                    check_one(row, hook)
            time.sleep(10)
        except Exception:
            traceback.print_exc()
            time.sleep(30)


DASHBOARD = """<!doctype html><meta charset="utf-8"><title>watchd</title>
<style>body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#222}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}th,td{{border-bottom:1px solid #eee;padding:8px;text-align:left;vertical-align:top}}
th{{color:#666;font-weight:600}}.ok{{color:#0a7}}.alert{{color:#c00}}code{{background:#f5f5f5;padding:1px 4px}}
</style><h2>watchd</h2><p>每 30 秒刷新 · 共 {n} 个监控</p>
<table><tr><th>#</th><th>名称</th><th>选择器</th><th>当前值</th><th>状态</th><th>上次检查</th></tr>{rows}</table>
<h3>最近事件</h3><table><tr><th>时间</th><th>监控</th><th>类型</th><th>旧值</th><th>新值</th></tr>{events}</table>
<meta http-equiv="refresh" content="30">"""


def dashboard() -> str:
    with db() as conn:
        mons = conn.execute("SELECT * FROM monitors ORDER BY id").fetchall()
        evs = conn.execute(
            "SELECT e.*, m.name FROM events e JOIN monitors m ON m.id=e.monitor_id ORDER BY e.ts DESC LIMIT 20"
        ).fetchall()
    rows = "".join(
        f"<tr><td>{m['id']}</td><td>{m['name']}</td><td><code>{m['selector']}</code></td>"
        f"<td>{(m['last_value'] or '—')[:120]}</td>"
        f"<td class='{'ok' if m['last_status']=='ok' else 'alert'}'>{m['last_status']}</td>"
        f"<td>{time.strftime('%m-%d %H:%M:%S', time.localtime(m['last_check'])) if m['last_check'] else '—'}</td></tr>"
        for m in mons
    )
    events = "".join(
        f"<tr><td>{time.strftime('%m-%d %H:%M', time.localtime(e['ts']))}</td><td>{e['name']}</td>"
        f"<td>{e['kind']}</td><td>{(e['old_value'] or '—')[:80]}</td><td>{(e['new_value'] or '—')[:80]}</td></tr>"
        for e in evs
    )
    return DASHBOARD.format(n=len(mons), rows=rows or "<tr><td colspan=6>暂无</td></tr>",
                            events=events or "<tr><td colspan=5>暂无</td></tr>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, b'{"ok":true}')
        if parsed.path == "/":
            return self._send(200, dashboard().encode(), "text/html; charset=utf-8")
        if parsed.path == "/api/monitors":
            with db() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM monitors ORDER BY id")]
            return self._send(200, json.dumps(rows, ensure_ascii=False).encode())
        if parsed.path == "/api/events":
            limit = int((parse_qs(parsed.query).get("limit") or ["50"])[0])
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT e.*, m.name AS monitor FROM events e JOIN monitors m ON m.id=e.monitor_id "
                    "ORDER BY e.ts DESC LIMIT ?", (min(limit, 500),))]
            return self._send(200, json.dumps(rows, ensure_ascii=False).encode())
        return self._send(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/monitors":
            return self._send(404, b'{"error":"not found"}')
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            name, url, selector = data["name"], data["url"], data["selector"]
            interval = int(data.get("interval_s") or 300)
        except (KeyError, ValueError, json.JSONDecodeError):
            return self._send(400, b'{"error":"need name,url,selector"}')
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO monitors(name,url,selector,interval_s,created) VALUES (?,?,?,?,?)",
                (name, url, selector, max(interval, 60), time.time()),
            )
            mid = cur.lastrowid
        return self._send(201, json.dumps({"id": mid}, ensure_ascii=False).encode())

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/monitors/"):
            return self._send(404, b'{"error":"not found"}')
        try:
            mid = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return self._send(400, b'{"error":"bad id"}')
        with db() as conn:
            conn.execute("DELETE FROM monitors WHERE id=?", (mid,))
            conn.execute("DELETE FROM events WHERE monitor_id=?", (mid,))
        return self._send(200, b'{"deleted":true}')


def main() -> None:
    init_db()
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    print(f"[watchd] listening on 0.0.0.0:{PORT}, db={DB}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
