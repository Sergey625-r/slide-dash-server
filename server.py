#!/usr/bin/env python3
"""Сервер рейтинга для Slide Dash. Только стандартная библиотека Python 3.8+.

Запуск:   python3 server.py
Настройки через переменные окружения:
  PORT     порт (по умолчанию 8080; хостинги вроде Render задают сами)
  API_KEY  общий ключ; тот же должен быть в клиенте (API_KEY в global.gd)
  DB_PATH  путь к файлу базы (по умолчанию scores.db)

Эндпоинты:
  GET  /            -> "ok"
  GET  /top?limit=50&id=<device_id>   -> {"top":[{"name","score","me"}], "me":{"rank","score"}|null, "total":N}
  POST /submit  {"id":"...", "name":"...", "score":1234}  -> {"ok":true, "best":N}
"""
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ.get("API_KEY", "change-me")
DB_PATH = os.environ.get("DB_PATH", "scores.db")
MAX_SCORE = 200000          # максимум метров, защита от явных накруток
RATE_LIMIT = 40             # запросов в минуту с одного IP

lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS scores (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    score INTEGER NOT NULL,
    updated INTEGER NOT NULL)""")
db.execute("CREATE INDEX IF NOT EXISTS idx_score ON scores(score DESC)")
db.commit()

hits = {}


def rate_ok(ip):
    now = time.time()
    arr = [t for t in hits.get(ip, []) if now - t < 60]
    arr.append(now)
    hits[ip] = arr
    if len(hits) > 5000:
        hits.clear()
    return len(arr) <= RATE_LIMIT


def clean_name(raw):
    s = re.sub(r"[\x00-\x1f\x7f]", "", str(raw))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:16].strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "SlideDash/1.0"

    def _send(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        if not rate_ok(ip):
            self._send(429, {"error": "too many requests"})
            return False
        if self.headers.get("X-Key", "") != API_KEY:
            self._send(403, {"error": "bad key"})
            return False
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            data = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(data)
            return
        if url.path != "/top":
            self._send(404, {"error": "not found"})
            return
        if not self._auth():
            return
        q = parse_qs(url.query)
        try:
            limit = max(1, min(100, int(q.get("limit", ["50"])[0])))
        except ValueError:
            limit = 50
        me_id = q.get("id", [""])[0][:64]
        with lock:
            rows = db.execute(
                "SELECT id, name, score FROM scores ORDER BY score DESC, updated ASC LIMIT ?",
                (limit,)).fetchall()
            total = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
            me = None
            if me_id:
                r = db.execute("SELECT score FROM scores WHERE id = ?", (me_id,)).fetchone()
                if r:
                    rank = db.execute("SELECT COUNT(*) + 1 FROM scores WHERE score > ?", (r[0],)).fetchone()[0]
                    me = {"rank": rank, "score": r[0]}
        top = [{"name": n, "score": s, "me": (i == me_id)} for i, n, s in rows]
        self._send(200, {"top": top, "me": me, "total": total})

    def do_POST(self):
        if urlparse(self.path).path != "/submit":
            self._send(404, {"error": "not found"})
            return
        if not self._auth():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2048:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            pid = str(body["id"])[:64]
            name = clean_name(body["name"])
            score = int(body["score"])
        except Exception:
            self._send(400, {"error": "bad request"})
            return
        if len(pid) < 8 or len(name) < 2 or score < 0 or score > MAX_SCORE:
            self._send(400, {"error": "invalid data"})
            return
        with lock:
            db.execute(
                """INSERT INTO scores (id, name, score, updated) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     name = excluded.name,
                     updated = excluded.updated,
                     score = MAX(scores.score, excluded.score)""",
                (pid, name, score, int(time.time())))
            db.commit()
            best = db.execute("SELECT score FROM scores WHERE id = ?", (pid,)).fetchone()[0]
        self._send(200, {"ok": True, "best": best})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print("Slide Dash leaderboard on port", PORT, "| API_KEY is", "default (change it!)" if API_KEY == "change-me" else "set")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
      
