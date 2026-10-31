#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日迹 · 本地写作服务.

    python3 scripts/serve.py --data ./data --port 8888

打开 http://127.0.0.1:8888 就能直接在页面上写日记: 写到磁盘、自动重绘日历.
只监听本机, 不上传任何内容.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import re
import uuid
import subprocess
import sys
import urllib.parse
import urllib.request
import webbrowser
import socket
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 1.0 纯本地版：无账号系统
accounts = None
import journal  # noqa: E402
import render  # noqa: E402

MAX_BODY = 2 * 1024 * 1024
START_TS = time.time()
# 打包脚本会把云端那份的这个改成 True：不存照片、不接上传
CLOUD_BUILD = False

# 配了就把验证码交给独立服务，不配就用内置的
CODESVC_URL = (os.environ.get("CODESVC_URL") or "").rstrip("/")
CODESVC_APP_ID = os.environ.get("CODESVC_APP_ID") or "riji"
CODESVC_KEY = os.environ.get("CODESVC_KEY") or ""
APP_NAME = "日迹"
APP_VERSION = "0.1"


def _adapters_from_ipconfig():
    """返回 [(网卡名, [ip...]), ...]，直接从系统读，不解析主机名。"""
    import subprocess
    out = []
    try:
        res = subprocess.run(["ipconfig"], capture_output=True, timeout=6,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = res.stdout.decode("gbk", "ignore") if res.stdout else ""
        cur = None
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.endswith(":") and ("adapter" in stripped or "适配器" in stripped):
                cur = stripped.split("adapter")[-1].strip().rstrip(":")
                cur = cur.split("适配器")[-1].strip().rstrip(":")
                out.append((cur, []))
                continue
            if "IPv4" in line and out:
                part = line.split(":", 1)[-1].strip().split("(")[0].strip()
                if part.count(".") == 3:
                    out[-1][1].append(part)
    except Exception:
        pass
    return [(n, ips) for n, ips in out if ips]


def _fallback_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    for getter in (
        lambda: [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)],
        lambda: socket.gethostbyname_ex(socket.gethostname())[2],
    ):
        try:
            for ip in getter():
                if ip and not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
    return ips


def lan_detail():
    """[(网卡名, ip), ...]"""
    rows = [(name, ip) for name, ips in _adapters_from_ipconfig() for ip in ips]
    if not rows:
        rows = [("", ip) for ip in _fallback_ips()]
    return rows


def lan_ips():
    """列出本机所有可能的局域网地址（按可连通性排序）。"""
    ips = []
    for _, ip in lan_detail():
        if ip not in ips:
            ips.append(ip)
    return _rank_ips(ips) if ips else []


def _rank_ips(ips):
    ips = [ip for ip in ips if not ip.startswith("169.254") and not ip.startswith("127.")]

    # 这些是虚拟网卡（VirtualBox / Windows 热点 / Docker），手机永远连不上
    VIRTUAL = ("192.168.56.", "192.168.137.", "192.168.99.", "192.168.65.", "192.168.49.")

    def rank(ip):
        if ip.startswith(VIRTUAL) or ip.startswith("172.17.") or ip.startswith("172.18."):
            return 9
        if ip.startswith("192.168."):
            return 0     # 安卓热点 / 家用路由
        if ip.startswith("172."):
            return 0     # iPhone 热点常是 172.20.10.x
        if ip.startswith("10."):
            return 1     # 校园 / 公司大网
        return 3

    return sorted(ips, key=rank)


def build_page(data_dir: Path, title: str, extra: dict = None) -> bytes:
    payload = journal.build_payload(data_dir, title)
    if extra:
        payload.update(extra)
    return render.build_html(payload).encode("utf-8")


class RijiServer(ThreadingHTTPServer):
    daemon_threads = True      # 请求线程随进程结束，不会越积越多
    allow_reuse_address = True
    request_queue_size = 64


def make_server(addr, handler):
    return RijiServer(addr, handler)


class Handler(BaseHTTPRequestHandler):
    data_dir = journal.DEFAULT_DATA
    base_dir = journal.DEFAULT_DATA
    accounts_db = None
    cloud = False
    auth_hits = {}
    auth_lock = threading.Lock()
    _last_mail = {}
    web_dir = Path(__file__).resolve().parent / "web"
    title = "日迹"
    who = "owner"          # 这个窗口是「谁」：owner / whale
    diaries = {}
    diary_order = []
    lan = False
    last_remote = {"at": "", "ip": ""}

    # ---------- 云端：认人 ----------
    def _cookie_diary(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "riji_diary":
                return urllib.parse.unquote(v)
        return ""

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "riji_token":
                return v
        return ""

    def _current_user(self):
        if not self.cloud or self.accounts_db is None:
            return None
        return accounts.user_by_token(self.accounts_db, self._cookie_token())

    def _enter(self):
        """每个请求先确定"这是谁的数据"。返回 (uid, email) 或 None。"""
        if not self.cloud:
            self.data_dir = self.base_dir
            sel = self._cookie_diary()
            if sel and sel in Handler.diaries:
                self.data_dir = Handler.diaries[sel]
            return (0, "")
        who = self._current_user()
        if not who:
            self.data_dir = self.base_dir
            return None
        uid, email = who
        self.data_dir = self.base_dir / "users" / str(uid)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return who

    def _codesvc(self, path, obj):
        """调独立验证码服务。连不上就返回一个 ok=False 的字典，不抛异常。"""
        import urllib.error
        try:
            req = urllib.request.Request(
                CODESVC_URL + path,
                data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "X-App-Id": CODESVC_APP_ID, "X-App-Key": CODESVC_KEY})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "error": "验证码服务返回异常"}
        except Exception:
            return {"ok": False, "error": "连不上验证码服务"}

    def _origin(self):
        host = self.headers.get("Host") or ("127.0.0.1:%d" % self.server.server_address[1])
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return "%s://%s" % (proto, host)

    def _set_token(self, token):
        self._pending_cookie = ("riji_token=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                                % (token, accounts.TOKEN_DAYS * 86400))

    def _login_page(self):
        page = (self.web_dir / "login.html").read_bytes()
        self._send(200, page, "text/html; charset=utf-8")

    timeout = 30          # 单个连接最多占 30 秒，防止慢连接堆死线程

    def log_message(self, fmt, *args):  # 安静一点
        pass

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        zipped = False
        try:
            ae = self.headers.get("Accept-Encoding") or ""
            if body and len(body) > 1024 and "gzip" in ae:
                import gzip as _gz
                body = _gz.compress(body, 6)
                zipped = True
        except Exception:
            zipped = False
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        tok = getattr(self, "_pending_cookie", None)
        if tok:
            self.send_header("Set-Cookie", tok)
            self._pending_cookie = None
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: dict):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _send_file(self, path: Path):
        if not path.is_file():
            return self._json(404, {"ok": False, "error": "not found"})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            ctype += "; charset=utf-8"
        cache = "public, max-age=31536000, immutable" if ctype.startswith("image/") else "no-store"
        self._send(200, path.read_bytes(), ctype, cache)

    def do_GET(self):
        path_only = self.path.split("?")[0]
        who = self._enter()
        if who is None:
            if path_only in ("/", "/index.html"):
                return self._login_page()
            if path_only.startswith(("/icons/", "/sw.js", "/manifest.json")):
                pass
            else:
                return self._json(401, {"ok": False, "error": "login required"})
        client = self.client_address[0] if self.client_address else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            Handler.last_remote = {
                "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "ip": client,
            }
        if path_only in ("/", "/index.html"):
            try:
                who = getattr(self, "_who", None) or self._current_user()
                extra = {"cloud": self.cloud, "email": (who[1] if who else ""), "who": self.who}
                self._send(200, build_page(self.data_dir, self.title, extra), "text/html; charset=utf-8")
            except Exception as exc:  # 页面构建失败也要能看到原因
                self._send(500, ("<pre>build failed: %s</pre>" % exc).encode("utf-8"),
                           "text/html; charset=utf-8")
        elif self.path == "/api/export":
            return self._export()
        elif path_only == "/api/version":
            try:
                stamp = (self.web_dir / "index.html").stat().st_mtime_ns
            except OSError:
                stamp = 0
            return self._json(200, {"ok": True, "v": str(stamp)})
        elif self.path == "/api/ping":
            self._json(200, {"ok": True, "days": len(journal.load_all(self.data_dir)), "who": self.who})
        elif path_only == "/api/diaries":
            names = Handler.diary_order or ["当前"]
            cur = self._cookie_diary()
            if not cur:
                for n, d in Handler.diaries.items():
                    if d == self.base_dir:
                        cur = n
                        break
            cur = cur or names[0]
            out = []
            for n in names:
                d = Handler.diaries.get(n, self.base_dir)
                try:
                    cnt = len(journal.load_all(d))
                except Exception:
                    cnt = 0
                out.append({"name": n, "count": cnt})
            self._json(200, {"ok": True, "current": cur, "list": out})
        elif path_only == "/api/switch":
            from urllib.parse import parse_qs as _pq, urlparse as _up
            q = _pq(_up(self.path).query)
            name = (q.get("d") or [""])[0]
            if name in Handler.diaries:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 "riji_diary=" + urllib.parse.quote(name) + "; Path=/; Max-Age=31536000")
                self.end_headers()
                return
            self._json(400, {"ok": False, "error": "unknown diary"})
        elif path_only == "/api/chat":
            from urllib.parse import parse_qs as _pq, urlparse as _up
            q = _pq(_up(self.path).query)
            ch = (q.get("channel") or ["sisters"])[0]
            store = Path(self.base_dir).parent.parent / "共用" / "聊天" / "messages.jsonl"
            items = []
            try:
                for line in store.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        try:
                            items.append(json.loads(line))
                        except Exception:
                            pass
            except OSError:
                pass
            items = [x for x in items if x.get("channel") == ch][-200:]
            return self._json(200, {"ok": True, "channel": ch, "messages": items})
        elif path_only == "/chat":
            page = self.web_dir / "chat.html"
            if not page.exists():
                return self._json(404, {"ok": False, "error": "chat.html missing"})
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        elif path_only == "/api/collections":
            cdir = Path(self.base_dir).parent.parent / "收藏室"
            out = []
            if cdir.exists():
                for sub in sorted([p for p in cdir.iterdir() if p.is_dir()], reverse=True):
                    idx = sub / "index.json"
                    try:
                        m = json.loads(idx.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    imgs = []
                    for img in (m.get("images") or []):
                        imgs.append({
                            "file": img.get("file"),
                            "message": img.get("message", ""),
                            "like": img.get("like"),
                            "uname": img.get("uname", ""),
                            "url": "/collections/img/%s/%s" % (sub.name, img.get("file")),
                        })
                    if imgs:
                        out.append({"folder": sub.name, "title": m.get("title") or m.get("bvid"), "images": imgs})
            return self._json(200, {"ok": True, "items": out})
        elif path_only == "/collections":
            page = self.web_dir / "collections.html"
            if not page.exists():
                return self._json(404, {"ok": False, "error": "collections.html missing"})
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        elif path_only.startswith("/collections/img/"):
            rel = path_only[len("/collections/img/"):]
            cdir = (Path(self.base_dir).parent.parent / "收藏室").resolve()
            target = (cdir / rel).resolve()
            if not str(target).startswith(str(cdir)) or not target.is_file():
                return self._json(404, {"ok": False, "error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)
        elif path_only == "/api/stickers":
            sdir = Path(self.base_dir).parent.parent.parent / "小肥鱼" / "表情包"
            items = []
            if sdir.exists():
                exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
                for f in sorted(sdir.iterdir()):
                    if f.is_file() and f.suffix.lower() in exts:
                        items.append({"name": f.name, "url": "/stickers/img/" + urllib.parse.quote(f.name)})
            self._json(200, {"ok": True, "items": items})
        elif path_only == "/stickers":
            page = self.web_dir / "stickers.html"
            if not page.exists():
                return self._json(404, {"ok": False, "error": "stickers.html missing"})
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        elif path_only.startswith("/stickers/img/"):
            rel = urllib.parse.unquote(path_only[len("/stickers/img/"):])
            sdir = (Path(self.base_dir).parent.parent.parent / "小肥鱼" / "表情包").resolve()
            target = (sdir / rel).resolve()
            if not str(target).startswith(str(sdir)) or not target.is_file():
                return self._json(404, {"ok": False, "error": "not found"})
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._send(200, target.read_bytes(), ctype)
        elif self.path.startswith("/api/net"):
            ips = lan_ips()
            named = {}
            for name, ip in lan_detail():
                named.setdefault(ip, name)
            self._json(200, {
                "ok": True,
                "port": self.server.server_address[1],
                "lan": self.lan,
                "iface": named.get(ips[0], "") if ips else "",
                "ips": ips,
                "urls": ["http://%s:%d/" % (ip, self.server.server_address[1]) for ip in ips],
                "remote": self.last_remote,
            })
        elif path_only == "/api/health":
            info = {"ok": True, "uptime": int(time.time() - START_TS),
                    "threads": threading.active_count()}
            info["rss_kb"] = 0
            try:
                import psutil
                info["rss_kb"] = psutil.Process().memory_info().rss // 1024
            except Exception:
                try:
                    with open("/proc/self/status", "r") as fh:
                        for line in fh:
                            if line.startswith("VmRSS:"):
                                info["rss_kb"] = int(line.split()[1])
                                break
                except Exception:
                    pass
            try:
                if self.accounts_db:
                    conn = accounts._connect(self.accounts_db)
                    info["users"] = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
                    info["sessions"] = conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"]
                    conn.close()
            except Exception:
                pass
            return self._json(200, info)
        elif path_only == "/api/day":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("d") or [""])[0]
            try:
                day = journal.parse_date(key)
            except SystemExit:
                return self._json(400, {"ok": False, "error": "bad date"})
            path = journal.day_path(self.data_dir, day)
            item = journal.parse_entry(path) if path.exists() else None
            if not item:
                return self._json(404, {"ok": False, "error": "none"})
            item["html"] = journal.md_to_html(item.get("body_md") or "")
            item.pop("dateobj", None)
            return self._json(200, {"ok": True, "entry": item})
        elif path_only == "/api/periods":
            return self._json(200, {"ok": True, "years": journal.list_periods(self.data_dir)})
        elif path_only == "/api/reports":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            if name:
                text = journal.read_report(self.data_dir, name)
                if not text:
                    return self._json(404, {"ok": False, "error": "没有这份回顾"})
                return self._json(200, {"ok": True, "name": Path(name).name, "text": text})
            return self._json(200, {"ok": True, "items": journal.list_reports(self.data_dir)})
        elif path_only == "/api/onthisday":
            return self._json(200, {"ok": True, "items": journal.onthisday(self.data_dir)})
        elif path_only == "/api/tags":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            latest = journal.read_tag_snapshot(self.data_dir, name)
            if not latest:
                latest = journal.tag_stats(self.data_dir)
            return self._json(200, {"ok": True, "latest": latest,
                                    "history": journal.tag_snapshots(self.data_dir)})
        elif path_only == "/api/search":
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("q") or [""])[0]
            mode = (q.get("mode") or ["any"])[0]
            try:
                limit = max(1, min(50, int((q.get("n") or ["20"])[0])))
            except ValueError:
                limit = 20
            kws = [k for k in re.split(r"[\s,，、]+", raw) if k]
            items = journal.search_entries(self.data_dir, kws, mode, limit)
            return self._json(200, {"ok": True, "q": raw, "mode": mode, "items": items})
        elif path_only == "/api/gc":
            removed = journal.gc_assets(self.data_dir)
            return self._json(200, {"ok": True, "removed": removed})
        elif path_only == "/api/mail":
            data = journal.deliver_due(self.data_dir)
            smtp = data.get("smtp") or {}
            data["smtp"] = {"host": smtp.get("host", ""), "port": smtp.get("port", ""),
                            "user": smtp.get("user", "")}
            data["today"] = dt.date.today().isoformat()
            return self._json(200, dict(ok=True, **data))
        elif self.path.startswith("/qr.png"):
            return self._qr()
        elif self.path == "/manifest.json":
            try:
                manifest = json.loads((self.web_dir / "manifest.json").read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
            manifest["name"] = self.title
            manifest["short_name"] = self.title
            return self._json(200, manifest)
        elif self.path == "/sw.js":
            body = (self.web_dir / "sw.js").read_bytes() if (self.web_dir / "sw.js").exists() else b""
            self._send(200, body, "application/javascript; charset=utf-8")
        elif self.path.startswith("/icons/"):
            rel = self.path[len("/icons/"):].split("?")[0]
            target = (self.web_dir / "icons" / rel).resolve()
            root = (self.web_dir / "icons").resolve()
            if not str(target).startswith(str(root)):
                return self._json(403, {"ok": False, "error": "forbidden"})
            self._send_file(target)
        elif self.path.startswith("/assets/"):
            rel = self.path[len("/assets/"):].split("?")[0]
            target = (self.data_dir / "assets" / rel).resolve()
            root = (self.data_dir / "assets").resolve()
            if not str(target).startswith(str(root)):
                return self._json(403, {"ok": False, "error": "forbidden"})
            self._send_file(target)
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path_only = self.path.split("?")[0]
        if path_only in ("/api/login", "/api/register", "/api/forgot", "/api/reset", "/api/send_code"):
            return self._auth(path_only)
        who = self._enter()
        if who is None:
            return self._json(401, {"ok": False, "error": "login required"})
        if path_only == "/api/chat":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json(400, {"ok": False, "error": "bad json"})
            text = (body.get("text") or "").strip()
            ch = body.get("channel") or "sisters"
            if not text:
                return self._json(400, {"ok": False, "error": "empty"})
            cdir = Path(self.base_dir).parent.parent / "共用" / "聊天"
            cdir.mkdir(parents=True, exist_ok=True)
            item = {
                "id": uuid.uuid4().hex[:10],
                "channel": ch,
                "from": body.get("from") or "主人",
                "text": text,
                "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(cdir / "messages.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            return self._json(200, {"ok": True, "id": item["id"]})
        if self.path == "/api/logout":
            accounts.logout(self.accounts_db, self._cookie_token())
            self._pending_cookie = "riji_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            return self._json(200, {"ok": True})
        if self.path == "/api/me":
            return self._json(200, {"ok": True, "email": who[1]})
        if self.path == "/api/export_mail":
            if not self.cloud:
                return self._json(400, {"ok": False, "error": "只有云端版支持发邮件"})
            if not self._rate_ok("export_mail"):
                return self._json(429, {"ok": False, "error": "太频繁了，过十分钟再试"})
            import time as _t
            last = getattr(Handler, "_last_mail", {}).get(who[0], 0)
            if _t.time() - last < 300:
                return self._json(429, {"ok": False, "error": "五分钟内只能发一次"})
            blob = self._export_bytes()
            name = "riji-%s.zip" % dt.date.today().strftime("%Y%m%d")
            ok = self._send_mail(
                who[1], "日迹 · 你的日记备份",
                "这封邮件里是你全部日记的压缩包（%s）。\n\n"
                "解压后是 Markdown 文件，可以直接用记事本打开。\n"
                "换电脑时，把这个包导回日迹即可。" % name,
                attachment=blob, filename=name)
            if not ok:
                return self._json(502, {"ok": False, "error": "服务器没配发信，暂时发不出去"})
            Handler._last_mail[who[0]] = _t.time()
            return self._json(200, {"ok": True})
        if self.path == "/api/delete_account":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                payload = {}
            uid, email = who
            if not accounts.check_password(self.accounts_db, uid, payload.get("password") or ""):
                return self._json(400, {"ok": False, "error": "密码不对"})
            accounts.delete_account(self.accounts_db, uid)
            import shutil
            shutil.rmtree(self.base_dir / "users" / str(uid), ignore_errors=True)
            self._pending_cookie = "riji_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            return self._json(200, {"ok": True})
        if self.path == "/api/upload":
            if CLOUD_BUILD:
                return self._json(403, {"ok": False, "error": "云端不存照片"})
            return self._upload()
        if self.path == "/api/summary":
            if CLOUD_BUILD:
                return self._json(403, {"ok": False, "error": "云端不提供 AI 总结"})
            return self._summary()
        if self.path == "/api/tags":
            stats = journal.save_tag_snapshot(self.data_dir)
            return self._json(200, {"ok": True, "latest": stats,
                                    "history": journal.tag_snapshots(self.data_dir)})
        if self.path == "/api/settings":
            return self._settings()
        if self.path == "/api/mail":
            return self._mail()
        if self.path == "/api/unlock":
            return self._unlock()
        if self.path == "/api/import":
            return self._import()
        if self.path == "/api/preview_md":
            return self._preview_md()
        if self.path == "/api/import_md":
            return self._import_md()
        if self.path != "/api/save":
            return self._json(404, {"ok": False, "error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"ok": False, "error": "body too large"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        body = (payload.get("body") or "").strip()
        photos = payload.get("photos") or []
        strokes = payload.get("strokes") or []

        try:
            day = journal.parse_date(payload.get("date") or "today")
        except SystemExit:
            return self._json(400, {"ok": False, "error": "日期看不懂"})

        path = journal.day_path(self.data_dir, day)

        # 只有用户明确点「清空这一天」才删除；普通保存绝不动已有内容
        if payload.get("clear") and not body and not photos and not strokes:
            if path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    return self._json(500, {"ok": False, "error": str(exc)})
            # 这一天自己的照片文件夹，整个删掉
            if not CLOUD_BUILD:
                try:
                    folder = self.data_dir / "assets" / day.isoformat()
                    if folder.is_dir():
                        import shutil as _sh
                        _sh.rmtree(folder, ignore_errors=True)
                    journal.gc_assets(self.data_dir)
                except Exception:
                    pass
            return self._json(200, {"ok": True, "date": day.isoformat(), "cleared": True})
        overwrite = bool(payload.get("overwrite"))
        if path.exists() and not overwrite and payload.get("append", True):
            old = journal.parse_entry(path)
            if old and old.get("body_md"):
                body = old["body_md"] + "\n\n" + body

        pw = (payload.get("lock_password") or "").strip()
        old_item = journal.parse_entry(path) if path.exists() else None
        old_author = str((old_item or {}).get("author") or "")
        # 谁写的谁改：别人写的正文只读，只能留言（force 留给迁移）
        if old_author and old_author != self.who:
            new_body = body.replace("\r\n", "\n").strip()
            old_body = (old_item.get("body_md") or "").replace("\r\n", "\n").strip()
            if new_body != old_body and not payload.get("force"):
                return self._json(403, {"ok": False, "error": "这是对方写的日记，正文不能改；你可以在下面留言。"})
        lock_hash = ""
        if pw:
            lock_hash = journal.hash_password(pw)
        elif payload.get("keep_lock") and old_item:
            lock_hash = old_item.get("lock_hash") or ""

        # 留言按「时间+内容+人」合并：对方新留的不会被这边一次保存抹掉
        old_comments = []
        if old_item:
            try:
                old_comments = json.loads(old_item.get("comments") or "[]")
            except Exception:
                old_comments = []
        if not isinstance(old_comments, list):
            old_comments = []

        def _ckey(c):
            return (str(c.get("at") or ""), str(c.get("text") or ""), str(c.get("by") or ""))

        dead = {_ckey(d) for d in (payload.get("comments_deleted") or []) if isinstance(d, dict)}
        merged = [c for c in old_comments if isinstance(c, dict) and _ckey(c) not in dead]
        seen = {_ckey(c) for c in merged}
        for c in (payload.get("comments") or []):
            if isinstance(c, dict) and _ckey(c) not in seen:
                merged.append(c)
                seen.add(_ckey(c))

        extra = {
            "author": old_author or self.who,
            "lock_hash": lock_hash,
            "lock_hint": (payload.get("lock_hint") or "").strip(),
            "deliver": (payload.get("deliver") or "").strip(),
            "images": payload.get("images") or [],
            "photos": [] if CLOUD_BUILD else (payload.get("photos") or []),
            "strokes": payload.get("strokes") or [],
            "comments": merged,
            "topic_times": payload.get("topic_times") or [],
            "topic_ids": payload.get("topic_ids") or [],
            "color": (payload.get("color") or "").strip(),
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 没有标题就用日期做标题，不要拿正文第一行去当标题 —— 会重复一遍
        title = (payload.get("title") or "").strip()

        path.parent.mkdir(parents=True, exist_ok=True)
        # 覆盖写的时候保留原来的 created（否则每存一次都像重新创建）
        created = (old_item.get("created") if old_item and old_item.get("created")
                   else dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
        text = journal.dump_entry(
            day,
            payload.get("mood") or "",
            payload.get("weather") or "",
            payload.get("tags") or [],
            title,
            body,
            created,
            extra=extra,
        )
        path.write_text(text, encoding="utf-8")
        try:
            journal.save_tag_snapshot(self.data_dir)
        except Exception:
            pass
        self._json(200, {"ok": True, "date": day.isoformat(), "file": str(path)})

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def _parse_md_files(self, files):
        items = []
        skipped = 0
        for item in files or []:
            name = str(item.get("name") or "")
            text = str(item.get("text") or "")
            raw = text
            if raw.lstrip().startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    raw = parts[2]
            yr = re.search(r"(20\d{2})", name)
            fallback_year = int(yr.group(1)) if yr else dt.date.today().year

            chunks = split_by_dates(raw, fallback_year)
            if not chunks:
                day = guess_day(name, text)
                if not day:
                    skipped += 1
                    continue
                chunks = [(day, raw.strip())]

            for day, body in chunks:
                body = body.strip()
                if not body:
                    skipped += 1
                    continue
                first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
                items.append({
                    "date": day.isoformat(),
                    "name": name,
                    "chars": len(body),
                    "preview": first[:40],
                    "body": body,
                })
        return items, skipped

    def _preview_md(self):
        payload = self._read_json()
        if payload is None:
            return self._json(400, {"ok": False, "error": "bad json"})
        items, skipped = self._parse_md_files(payload.get("files"))
        out = []
        for it in items:
            exists = journal.day_path(
                self.data_dir, dt.date.fromisoformat(it["date"])).exists()
            out.append({"date": it["date"], "name": it["name"],
                        "chars": it["chars"], "preview": it["preview"],
                        "exists": exists})
        self._json(200, {"ok": True, "items": out, "skipped": skipped})

    def _import_md(self):
        payload = self._read_json()
        if payload is None:
            return self._json(400, {"ok": False, "error": "bad json"})

        items, skipped = self._parse_md_files(payload.get("files"))
        stamp = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        saved = 0
        for it in items:
            day = dt.date.fromisoformat(it["date"])
            # 重新取一遍正文（preview 里只带了摘要）
            body = it.get("body") or ""
            if not body:
                continue
            path = journal.day_path(self.data_dir, day)
            if path.exists():
                old = journal.parse_entry(path)
                if old and old.get("body_md"):
                    body = old["body_md"] + "\n\n" + body
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(journal.dump_entry(day, "", "", [], "", body, stamp),
                            encoding="utf-8")
            saved += 1
        self._json(200, {"ok": True, "saved": saved, "skipped": skipped})

    def _qr(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        data = (q.get("d") or [""])[0][:500]
        if not data:
            return self._json(400, {"ok": False, "error": "missing d"})
        try:
            import qrcode
            import qrcode.image.pil  # noqa: F401
            img = qrcode.make(data, box_size=10, border=3)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self._send(200, buf.getvalue(), "image/png")
        except Exception:
            self._json(501, {"ok": False, "error": "没有二维码库"})

    def _export_bytes(self):
        buf = io.BytesIO()
        files = []
        days = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for sub in ("days", "assets"):
                base = self.data_dir / sub
                if not base.exists():
                    continue
                for p in base.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = str(Path(sub) / p.relative_to(base)).replace("\\", "/")
                    blob = p.read_bytes()
                    z.writestr(rel, blob)
                    files.append({
                        "path": rel,
                        "bytes": len(blob),
                        "sha256": hashlib.sha256(blob).hexdigest(),
                    })
                    if sub == "days" and rel.endswith(".md"):
                        days += 1
            manifest = {
                "app": APP_NAME,
                "version": APP_VERSION,
                "exported_at": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "days": days,
                "assets": len(files) - days,
                "files": files,
            }
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        return buf.getvalue()

    def _export(self):
        data = self._export_bytes()
        name = "riji-%s.zip" % dt.date.today().strftime("%Y%m%d")
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _import(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 200 * 1024 * 1024:
            return self._json(413, {"ok": False, "error": "文件太大"})
        raw = self.rfile.read(length)
        try:
            z = zipfile.ZipFile(io.BytesIO(raw))
        except Exception:
            return self._json(400, {"ok": False, "error": "这不是一个有效的 zip"})

        manifest = None
        try:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
        except KeyError:
            pass
        except Exception:
            manifest = None

        count = 0
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if len(parts) < 2 or parts[0] not in ("days", "assets"):
                continue
            base = (self.data_dir / parts[0]).resolve()
            target = (base / "/".join(parts[1:])).resolve()
            if not str(target).startswith(str(base)):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            count += 1
        self._json(200, {"ok": True, "count": count, "manifest": manifest})

    def _rate_ok(self, action):
        """同一个 IP，10 分钟内最多 10 次登录/注册/找回。"""
        ip = self.client_address[0] if self.client_address else "?"
        now = time.time()
        with Handler.auth_lock:
            hits = [t for t in Handler.auth_hits.get(ip, []) if now - t < 600]
            if len(hits) >= 10:
                Handler.auth_hits[ip] = hits
                return False
            hits.append(now)
            Handler.auth_hits[ip] = hits
            # 被大量 IP 刷的时候，整张表也要收口，别让它无限长
            if len(Handler.auth_hits) > 2000:
                for k in [k for k, v in Handler.auth_hits.items()
                          if not v or now - v[-1] > 600]:
                    Handler.auth_hits.pop(k, None)
                if len(Handler.auth_hits) > 4000:
                    Handler.auth_hits.clear()
        return True

    def _send_mail(self, to_addr, subject, body, attachment=None, filename=""):
        """发信：从环境变量读 SMTP。没配就返回 False，调用方决定怎么办。

        attachment 是字节，给了就作为附件发出去。
        """
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email.mime.multipart import MIMEMultipart
        from email import encoders
        from email.header import Header
        host = os.environ.get("RIJI_SMTP_HOST") or ""
        user = os.environ.get("RIJI_SMTP_USER") or ""
        if not host or not user:
            return False
        try:
            if attachment:
                msg = MIMEMultipart()
                msg.attach(MIMEText(body, "plain", "utf-8"))
                part = MIMEBase("application", "zip")
                part.set_payload(attachment)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment",
                                filename=("utf-8", "", filename or "riji.zip"))
                msg.attach(part)
            else:
                msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = user
            msg["To"] = to_addr
            port = int(os.environ.get("RIJI_SMTP_PORT") or 465)
            if port == 465:
                srv = smtplib.SMTP_SSL(host, port, timeout=12)
            else:
                srv = smtplib.SMTP(host, port, timeout=12)
                try:
                    srv.starttls()
                except Exception:
                    pass
            pw = os.environ.get("RIJI_SMTP_PASS") or ""
            if pw:
                srv.login(user, pw)
            srv.sendmail(user, [to_addr], msg.as_string())
            srv.quit()
            return True
        except Exception:
            return False

    def _auth(self, which):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        if not self._rate_ok(which):
            return self._json(429, {"ok": False, "error": "太频繁了，过十分钟再试"})

        if which == "/api/forgot":
            _, token = accounts.make_reset(self.accounts_db, payload.get("email") or "")
            mailed = False
            if token:
                link = "%s/#reset=%s" % (self._origin(), token)
                mailed = self._send_mail(
                    payload.get("email") or "", "日迹 · 重置密码",
                    "打开这个链接可以重设密码（两小时内有效）：\n\n" + link +
                    "\n\n如果不是你本人操作，忽略这封邮件即可。")
                if not mailed:
                    print("[日迹] 没配置 SMTP，重置链接：", link)
            # 不暴露邮箱是否存在；但要说清楚邮件到底发没发出去
            smtp_ready = bool(os.environ.get("RIJI_SMTP_HOST"))
            return self._json(200, {"ok": True, "sent": mailed, "smtp": smtp_ready})

        if which == "/api/reset":
            uid, err = accounts.reset_password(self.accounts_db, payload.get("token") or "",
                                               payload.get("password") or "")
            if not uid:
                return self._json(400, {"ok": False, "error": err})
            token = accounts.new_token(self.accounts_db, uid)
            self._set_token(token)
            return self._json(200, {"ok": True})

        email = payload.get("email") or ""
        password = payload.get("password") or ""
        if which == "/api/send_code":
            if CODESVC_URL:
                r = self._codesvc("/v1/code/send", {"target": email, "scene": "register"})
                if not r.get("ok"):
                    return self._json(502 if r.get("code_id") is None and "邮件" in (r.get("error") or "") else 400, r)
                return self._json(200, {"ok": True, "code_id": r.get("code_id") or ""})
            code, err = accounts.request_code(self.accounts_db, email)
            if err:
                return self._json(400, {"ok": False, "error": err})
            sent = self._send_mail(
                email, "日迹 · 注册验证码",
                "你的验证码是：%s\n\n10 分钟内有效。如果不是你本人操作，忽略这封邮件。" % code)
            if not sent:
                print("[日迹] 没配 SMTP，验证码是：", code, "->", email)
                return self._json(502, {"ok": False,
                                        "error": "邮件没发出去，请联系管理员（后台日志里有验证码）"})
            return self._json(200, {"ok": True})

        if which == "/api/register":
            if CODESVC_URL:
                v = self._codesvc("/v1/code/verify",
                                  {"code_id": payload.get("code_id") or "",
                                   "code": payload.get("code") or ""})
                if not v.get("ok"):
                    return self._json(400, {"ok": False, "error": v.get("error") or "验证码不对"})
                t = self._codesvc("/v1/ticket/consume", {"ticket": v.get("ticket") or ""})
                if not t.get("ok") or (t.get("target") or "") != email.strip().lower():
                    return self._json(400, {"ok": False, "error": "验证已失效，重新发一次"})
            else:
                okc, errc = accounts.check_code(self.accounts_db, email, payload.get("code") or "")
                if not okc:
                    return self._json(400, {"ok": False, "error": errc})
            uid, err = accounts.register(self.accounts_db, email, password)
        else:
            uid, err = accounts.login(self.accounts_db, email, password)
        if not uid:
            return self._json(400, {"ok": False, "error": err})
        token = accounts.new_token(self.accounts_db, uid)
        self._set_token(token)
        self.data_dir = self.base_dir / "users" / str(uid)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self._json(200, {"ok": True})

    def _settings(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})
        cur = journal.load_settings(self.data_dir)
        for k in ("api_base", "api_key", "model", "birthday"):
            if k in payload:
                val = str(payload.get(k) or "").strip()
                if k == "api_key" and val and set(val) == {"*"}:
                    continue
                cur[k] = val
        if "ai_enabled" in payload:
            cur["ai_enabled"] = bool(payload.get("ai_enabled"))
        journal.save_settings(self.data_dir, cur)
        safe = dict(cur)
        safe["api_key"] = ("*" * 8) if cur.get("api_key") else ""
        safe["ai_enabled"] = bool(cur.get("ai_enabled"))
        return self._json(200, dict(ok=True, **safe))

    def _summary(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        today = dt.date.today()
        anchor = (payload.get("anchor") or "").strip()
        if anchor:
            try:
                today = dt.datetime.strptime(anchor, "%Y-%m-%d").date()
            except ValueError:
                pass
        period = (payload.get("period") or "month").strip()
        if period not in ("week", "month", "year"):
            period = "month"
        try:
            year = int(payload.get("year") or today.year)
            month = int(payload.get("month") or today.month)
        except Exception:
            year, month = today.year, today.month

        if period == "week":
            start = today - dt.timedelta(days=today.weekday())
            end = start + dt.timedelta(days=6)
            iso_year, iso_week, _ = start.isocalendar()
            label = "%d 年第 %d 周（%s ~ %s）" % (
                iso_year, iso_week, start.strftime("%m-%d"), end.strftime("%m-%d"))
        elif period == "year":
            start, end = dt.date(year, 1, 1), dt.date(year, 12, 31)
            label = "%d 年" % year
        else:
            start = dt.date(year, month, 1)
            end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            label = "%d 年 %d 月" % (year, month)

        entries = journal.period_entries_range(self.data_dir, start, end)
        prompt = journal.summary_prompt(entries, label, period)
        settings = journal.load_settings(self.data_dir)
        has_key = bool((settings.get("api_key") or "").strip())
        enabled = bool(settings.get("ai_enabled"))
        text = ""
        error = ""
        saved = ""
        if period == "month" and end >= today:
            error = "这个月还没过完，%s 之后再生成" % (end + dt.timedelta(days=1)).strftime("%m-%d")
        elif period == "year" and end >= today:
            error = "今年还没过完，明年 1 月 1 号之后再生成"
        elif not entries:
            error = "这段时间没有可回顾的日记"
        elif not enabled:
            error = "AI 回顾未开启（设置 → AI 回顾）"
        elif not has_key:
            error = "还没填 API Key（设置 → AI 回顾）"
        else:
            try:
                text = journal.call_ai(settings, prompt)
                if not text:
                    error = "模型返回了空内容"
            except Exception as exc:
                error = str(exc)[:300]
        if text and payload.get("save") is not False:
            try:
                saved = journal.save_report(self.data_dir, period, start, text)
            except Exception as exc:
                saved = "保存失败：%s" % exc
        return self._json(200, {
            "ok": True, "period": period, "label": label,
            "start": start.isoformat(), "end": end.isoformat(),
            "days": len(entries), "prompt": prompt, "text": text,
            "has_key": has_key, "ai_enabled": enabled,
            "error": error, "saved": saved,
        })

    def _mail(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})

        data = journal.load_mail(self.data_dir)
        op = payload.get("op") or ""

        if op == "config":
            if "birthday" in payload:
                data["birthday"] = (payload.get("birthday") or "").strip()
            if "email" in payload:
                data["email"] = (payload.get("email") or "").strip()
            if "smtp" in payload:
                data["smtp"] = payload.get("smtp") or {}
        elif op == "create":
            body = (payload.get("body") or "").strip()
            deliver = (payload.get("deliver") or "").strip()
            if not body or not deliver:
                return self._json(400, {"ok": False, "error": "收信日期和内容都要有"})
            letter = {
                "id": dt.datetime.now().strftime("%Y%m%d%H%M%S%f"),
                "created": dt.date.today().isoformat(),
                "deliver": deliver,
                "subject": (payload.get("subject") or "").strip() or "给未来的自己",
                "body": body,
                "sent_at": "",
                "read_at": "",
            }
            data["letters"].insert(0, letter)
        elif op == "delete":
            lid = payload.get("id") or ""
            data["letters"] = [x for x in data["letters"] if x.get("id") != lid]
        elif op == "read":
            lid = payload.get("id") or ""
            for x in data["letters"]:
                if x.get("id") == lid:
                    x["read_at"] = dt.date.today().isoformat()
        else:
            return self._json(400, {"ok": False, "error": "unknown op"})

        journal.save_mail(self.data_dir, data)
        out = journal.deliver_due(self.data_dir)
        smtp = out.get("smtp") or {}
        out["smtp"] = {"host": smtp.get("host", ""), "port": smtp.get("port", ""),
                       "user": smtp.get("user", "")}
        out["today"] = dt.date.today().isoformat()
        return self._json(200, dict(ok=True, **out))

    def _unlock(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"ok": False, "error": "bad json"})
        try:
            day = journal.parse_date(payload.get("date") or "today")
        except SystemExit:
            return self._json(400, {"ok": False, "error": "日期看不懂"})
        item = journal.parse_entry(journal.day_path(self.data_dir, day))
        if not item:
            return self._json(404, {"ok": False, "error": "这一天没有日记"})
        if not item.get("lock_hash"):
            return self._json(400, {"ok": False, "error": "这一天没有上锁"})
        if not journal.check_password(item, payload.get("password") or ""):
            return self._json(403, {"ok": False, "error": "密码不对"})
        self._json(200, {
            "ok": True,
            "body_md": item.get("body_md") or "",
            "mood": item.get("mood") or "",
            "images": item.get("images") or "",
            "lock_hint": item.get("lock_hint") or "",
        })

    def _upload(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 12 * 1024 * 1024:
            return self._json(413, {"ok": False, "error": "图片太大（上限 12MB）"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            raw = base64.b64decode(str(payload.get("data") or "").split(",")[-1])
        except Exception:
            return self._json(400, {"ok": False, "error": "bad image"})

        ext = (Path(str(payload.get("name") or "photo.jpg")).suffix or ".jpg").lower()
        if ext not in (".jpeg", ".jpg", ".png", ".gif", ".webp", ".heic"):
            ext = ".jpg"
        # 照片属于某一天，就存到那一天自己的文件夹里
        day_key = str(payload.get("date") or "").strip()
        try:
            folder = journal.parse_date(day_key).isoformat()
        except Exception:
            folder = dt.date.today().isoformat()
        out_dir = self.data_dir / "assets" / folder
        out_dir.mkdir(parents=True, exist_ok=True)
        name = uuid.uuid4().hex[:12] + ext
        (out_dir / name).write_bytes(raw)
        self._json(200, {"ok": True, "path": folder + "/" + name})


EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


RE_DAY_LINE = re.compile(
    r"^\s*#{0,6}\s*(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(.*)$"
)
RE_MD_LINE = re.compile(r"^\s*#{0,6}\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(.*)$")


def split_by_dates(text: str, fallback_year=None):
    """把「一个本子」拆成一天一天：日期那行是分界，下面是那天。"""
    lines = text.replace("\r\n", "\n").split("\n")
    marks = []
    for i, line in enumerate(lines):
        m = RE_DAY_LINE.match(line)
        if m:
            try:
                d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            marks.append((i, d, (m.group(4) or "").strip(" \t-|,，。")))
            continue
        m = RE_MD_LINE.match(line)
        if m and fallback_year:
            try:
                d = dt.date(int(fallback_year), int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
            marks.append((i, d, (m.group(3) or "").strip(" \t-|,，。")))
    if len(marks) < 2:
        return []
    out = []
    for idx, (line_no, day, rest) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        chunk = lines[line_no + 1:end]
        if rest:
            chunk = [rest] + chunk
        if idx == 0:
            head = "\n".join(lines[:line_no]).strip()
            if head and len(head) < 300:
                chunk = [head] + [""] + chunk
        out.append((day, "\n".join(chunk).strip()))
    return out


def guess_day(name: str, text: str):
    """从文件名或正文里认出日期。认不出就返回 None。"""
    blob = name + "\n" + text[:500]
    m = re.search(r"(20\d{2})[-_./年](\d{1,2})[-_./月](\d{1,2})", blob)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", blob)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def probe(port: int) -> bool:
    """这个端口上是不是已经有一个日迹在跑?"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=0.6) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


WIN_W = (520, 760)
WIN_X = (508, 30)


def open_app(url: str) -> None:
    """用一个没有地址栏的窗口打开 - 看起来像个应用, 而不是一个端口."""
    exe = next((c for c in EDGE_CANDIDATES if os.path.exists(c)), None)
    if not exe:
        webbrowser.open(url)
        return
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    profile = Path(base) / "Riji" / (os.environ.get("RIJI_PROFILE") or "app")
    profile.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen([
            exe,
            "--app=" + url,
            "--window-size=%d,%d" % WIN_W,
            "--window-position=%d,%d" % WIN_X,
            "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=" + str(profile),
        ])
    except Exception:
        webbrowser.open(url)




def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="日迹 · 本地写作服务")
    parser.add_argument("--data", default=str(journal.DEFAULT_DATA))
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--title", default="日记·鱼")
    parser.add_argument("--who", default="owner", help="这个窗口是谁：owner（我）/ whale（鱼）")
    parser.add_argument("--diary", action="append", default=[], metavar="NAME=PATH",
                        help="多本日记：名字=数据目录，可重复")
    parser.add_argument("--lan", action="store_true", help="允许同一 WiFi 下的手机访问")
    parser.add_argument("--app", action="store_true", help="用无地址栏的窗口打开（像个应用）")
    parser.add_argument("--open", action="store_true", help="用默认浏览器打开")
    parser.add_argument("--cloud", action="store_true", help="云端模式：要登录，数据按用户分开")
    parser.add_argument("--reset-link", metavar="EMAIL", help="生成一个重置密码链接（没配 SMTP 时手动发）")
    args = parser.parse_args(argv)

    data_dir = Path(args.data).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    Handler.base_dir = data_dir
    Handler.data_dir = data_dir
    Handler.cloud = bool(args.cloud)
    Handler.who = args.who
    Handler.diaries = {}
    Handler.diary_order = []
    for item in args.diary:
        name, _, path = item.partition("=")
        if name and path:
            Handler.diaries[name] = Path(path).resolve()
            Handler.diary_order.append(name)
    if Handler.diaries:
        Handler.data_dir = Handler.diaries[Handler.diary_order[0]]
    if Handler.cloud:
        Handler.accounts_db = data_dir / "accounts.db"
        accounts.init(Handler.accounts_db)

    if args.reset_link:
        Handler.accounts_db = Handler.accounts_db or (data_dir / "accounts.db")
        accounts.init(Handler.accounts_db)
        uid, token = accounts.make_reset(Handler.accounts_db, args.reset_link)
        if not token:
            print("没有这个邮箱：%s" % args.reset_link)
            return 1
        print("两小时内有效：")
        print("  http://你的域名/#reset=%s" % token)
        return 0
    Handler.title = args.title

    # 已经有一个在跑? 直接把窗口叫出来, 不再起第二个.
    port = args.port
    if probe(port):
        url = "http://127.0.0.1:%d" % port
        if args.app:
            open_app(url)
        elif args.open:
            webbrowser.open(url)
        print("日迹已经在运行: %s" % url)
        return 0

    bind = "0.0.0.0" if args.lan else "127.0.0.1"
    Handler.lan = bool(args.lan)
    for _ in range(12):
        try:
            httpd = make_server((bind, port), Handler)
            break
        except OSError:
            port += 1
    else:
        print("找不到可用端口")
        return 1

    url = "http://127.0.0.1:%d" % port
    print("日迹已启动: %s" % url)
    if args.lan:
        for ip in lan_ips():
            print("  手机（同一个 WiFi）: http://%s:%d/" % (ip, port))
    print("数据目录: %s" % data_dir)
    print("Ctrl+C 停止")
    if args.app:
        open_app(url)
    elif args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
