#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日迹 / Riji - 一天一格的一页式日记.

一个 Skill 的核心 CLI: 把口述变成文件, 把文件变成日历, 把日历变成回顾.

用法:
    python3 scripts/riji.py add --mood 平静 --weather 晴 --body-file body.md
    python3 scripts/riji.py add --date 2026-10-22 --title "..." --body "..."
    python3 scripts/riji.py build --open
    python3 scripts/riji.py list
    python3 scripts/riji.py stats
    python3 scripts/riji.py export --year 2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import hmac
import re
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = APP_ROOT.parent / "data"


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def parse_date(value: str) -> dt.date:
    value = (value or "").strip()
    if not value or value.lower() in ("today", "今天", "now"):
        return dt.date.today()
    value = value.replace("/", "-").replace(".", "-")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if m:
        today = dt.date.today()
        return dt.date(today.year, int(m.group(1)), int(m.group(2)))
    raise SystemExit("无法识别的日期: %s (请用 2026-10-22 格式)" % value)


def day_path(data_dir: Path, day: dt.date) -> Path:
    return data_dir / "days" / day.strftime("%Y-%m") / (day.isoformat() + ".md")


def count_words(text: str) -> int:
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z0-9]+", text))
    return cjk + latin


# ---------------------------------------------------------------- 读写文件

def dump_entry(day: dt.date, mood: str, weather: str, tags: list,
               title: str, body: str, created: str, extra=None) -> str:
    body = body.replace("\r\n", "\n").strip()
    tags_line = ", ".join(t.strip().lstrip("#") for t in tags if t.strip())
    head = [
        "---",
        "date: " + day.isoformat(),
        "mood: " + mood.strip(),
        "weather: " + weather.strip(),
        "tags: " + tags_line,
        "words: " + str(count_words(body)),
        "created: " + created,
    ]
    if extra:
        if extra.get("lock_hash"):
            head.append("lock_hash: " + str(extra["lock_hash"]))
        if extra.get("lock_hint"):
            head.append("lock_hint: " + str(extra["lock_hint"]))
        if extra.get("deliver"):
            head.append("deliver: " + str(extra["deliver"]))
        if extra.get("images"):
            imgs = extra["images"]
            head.append("images: " + (", ".join(imgs) if isinstance(imgs, list) else str(imgs)))
        if extra.get("photos"):
            head.append("photos: " + json.dumps(extra["photos"], ensure_ascii=False, separators=(",", ":")))
        if extra.get("updated"):
            head.append("updated: " + str(extra["updated"]))
        if extra.get("author"):
            head.append("author: " + str(extra["author"]))
        if extra.get("comments"):
            head.append("comments: " + json.dumps(extra["comments"], ensure_ascii=False, separators=(",", ":")))
        if extra.get("color"):
            head.append("color: " + str(extra["color"]))
        ids = extra.get("topic_ids")
        if isinstance(ids, list) and ids:
            head.append("topic_ids: " + json.dumps([str(t) for t in ids], ensure_ascii=False, separators=(",", ":")))
        times = extra.get("topic_times")
        if isinstance(times, list) and any(str(t).strip() for t in times):
            head.append("topic_times: " + json.dumps([str(t) for t in times], ensure_ascii=False, separators=(",", ":")))
        if extra.get("strokes"):
            head.append("strokes: " + json.dumps(extra["strokes"], ensure_ascii=False, separators=(",", ":")))
    head += [
        "---",
        "",
        "# " + (title.strip() or day.strftime("%Y年%m月%d日")),
        "",
        body,
        "",
    ]
    return "\n".join(head)


def parse_entry(path: Path):
    try:
        raw = path.read_text(encoding="utf-8-sig")  # 有 BOM 也不能把 frontmatter 当正文
    except Exception:
        return None
    meta = {"date": "", "mood": "", "weather": "", "tags": "", "words": "0", "created": "", "topic_ids": "",
            "lock_hash": "", "lock_hint": "", "deliver": "", "images": "", "photos": "", "strokes": "", "comments": "", "updated": "", "color": "", "topic_times": "", "author": ""}
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            fm, body = parts[1], parts[2]
            for line in fm.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k = k.strip()
                    if k in meta:
                        meta[k] = v.strip()
    title = ""
    lines = body.strip("\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    body_md = "\n".join(lines).strip()
    text = strip_md(body_md)
    meta["title"] = title
    meta["body_md"] = body_md
    meta["excerpt"] = make_excerpt(first_paragraph(body_md))
    meta["text"] = text
    meta["words"] = str(count_words(text))
    meta["time"] = (meta.get("created") or "")[11:16]
    try:
        meta["dateobj"] = dt.date.fromisoformat(path.stem)
    except ValueError:
        return None
    meta["day"] = path.stem
    return meta


def strip_md(md: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", md, flags=re.M)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"\x60(.+?)\x60", r"\1", text)
    text = re.sub(r"\[(.+?)\]\((.+?)\)", r"\1", text)
    text = re.sub(r"!\[(.*?)\]\((.+?)\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def first_paragraph(md: str) -> str:
    """跳过小标题, 取第一段真正的正文 - 用作日历格子里的摘要."""
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        s = strip_md(s)
        if s:
            return s
    return ""


def make_excerpt(text: str, limit: int = 46) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，,。.;；") + "…"


# ---------------------------------------------------------------- markdown 渲染

def md_to_html(md: str) -> str:
    """极简 markdown -> html, 只覆盖日记会用到的语法."""
    out, in_list, in_quote, para = [], False, False, []

    def flush_para():
        if para:
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            para.clear()

    def close_blocks():
        nonlocal in_list, in_quote
        if in_list:
            out.append("</ul>")
            in_list = False
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    for raw_line in md.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            flush_para()
            close_blocks()
            continue
        if re.match(r"^#{1,6}\s", line):
            flush_para()
            close_blocks()
            level = len(line) - len(line.lstrip("#"))
            tag = min(level + 2, 6)  # ## -> h4, ### -> h5 (h1 已被标题占用)
            out.append("<h%d>%s</h%d>" % (tag, inline(line[level:].strip()), tag))
            continue
        if re.match(r"^\s*([-*+])\s+", line):
            flush_para()
            if in_quote:
                out.append("</blockquote>")
                in_quote = False
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>" + inline(re.sub(r"^\s*([-*+])\s+", "", line)) + "</li>")
            continue
        if re.match(r"^\s*\d+\.\s+", line):
            flush_para()
            if in_quote:
                out.append("</blockquote>")
                in_quote = False
            if not in_list:
                out.append('<ul class="ordered">')
                in_list = True
            out.append("<li>" + inline(re.sub(r"^\s*\d+\.\s+", "", line)) + "</li>")
            continue
        if line.startswith(">"):
            flush_para()
            if in_list:
                out.append("</ul>")
                in_list = False
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append("<p>" + inline(line.lstrip("> ")) + "</p>")
            continue
        if re.match(r"^\s*(---|\*\*\*)\s*$", line):
            flush_para()
            close_blocks()
            out.append("<hr>")
            continue
        if in_list or in_quote:
            close_blocks()
        para.append(line.strip())
    flush_para()
    close_blocks()
    return "\n".join(out)


def inline(text: str) -> str:
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\x60(.+?)\x60", r"<code>\1</code>", text)
    text = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2" target="_blank">\1</a>', text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    return text


# ---------------------------------------------------------------- 数据汇总

# ---------------------------------------------------------------- 信箱
MAIL_NAME = "mail.json"


def mail_path(data_dir: Path) -> Path:
    return data_dir / MAIL_NAME


def load_mail(data_dir: Path) -> dict:
    path = mail_path(data_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("birthday", "")
                data.setdefault("email", "")
                data.setdefault("letters", [])
                data.setdefault("smtp", {})
                data.setdefault("birthday_sent", "")
                return data
        except Exception:
            pass
    return {"birthday": "", "email": "", "letters": [], "smtp": {}, "birthday_sent": ""}


def save_mail(data_dir: Path, data: dict) -> None:
    try:
        mail_path(data_dir).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def send_mail_via_smtp(conf: dict, to_addr: str, subject: str, body: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    host = (conf or {}).get("host") or ""
    if not host or not to_addr:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = conf.get("user") or to_addr
        msg["To"] = to_addr
        port = int(conf.get("port") or 465)
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            try:
                server.starttls()
            except Exception:
                pass
        if conf.get("user"):
            server.login(conf["user"], conf.get("pass") or "")
        server.sendmail(conf.get("user") or to_addr, [to_addr], msg.as_string())
        server.quit()
        return True
    except Exception:
        return False


def birthday_letter_body(data_dir: Path, today: dt.date) -> str:
    """生日信：用这个人自己写过的东西来写，不套模板。"""
    lines = ["今天是你的生日。", ""]
    try:
        entries = load_all(data_dir)
    except Exception:
        entries = []
    opened = [e for e in entries if not e.get("sealed")]
    start = today - dt.timedelta(days=365)
    year = [e for e in opened if start <= e["dateobj"] <= today]
    if year:
        words = sum(int(e.get("words") or 0) for e in year)
        _, best = streaks({e["dateobj"] for e in opened}, today)
        lines.append("过去一年，你写下了 %d 天、%d 字。" % (len(year), words))
        if best > 1:
            lines.append("最长的一次，连着写了 %d 天。" % best)
        # 写得最多的那个月
        months = {}
        for e in year:
            key = e["dateobj"].month
            months[key] = months.get(key, 0) + 1
        if months:
            top_m = max(months, key=months.get)
            lines.append("写得最多的是 %d 月，一共 %d 天。" % (top_m, months[top_m]))
        # 最常写下的心情
        moods = {}
        for e in year:
            m = (e.get("mood") or "").strip()
            if m:
                moods[m] = moods.get(m, 0) + 1
        if moods:
            top_mood = max(moods, key=moods.get)
            lines.append("这一年，你最常写下的心情是「%s」（%d 次）。" % (top_mood, moods[top_mood]))
        lines.append("")
    # 去年今天的原文
    quote = None
    for back in (0, 1, -1, 2, -2, 3, -3):
        try:
            probe = today.replace(year=today.year - 1) + dt.timedelta(days=back)
        except ValueError:
            continue
        for e in opened:
            if e["dateobj"] == probe:
                quote = e
                break
        if quote:
            break
    if quote:
        text = quote.get("excerpt") or ""
        if text:
            lines.append("一年前的今天，你写的是：")
            lines.append("「" + text.strip() + "」")
            lines.append("")
    if not year:
        lines.append("这是你在日迹里的第一个生日。")
        lines.append("")
    lines.append("愿你今年也有想记下来的日子。")
    return "\n".join(lines)


def deliver_due(data_dir: Path) -> dict:
    """打开信箱时投递：到日子的信标为已送达；生日当天自动写一封。"""
    data = load_mail(data_dir)
    today = dt.date.today()
    today_s = today.isoformat()
    changed = False
    just = []

    for letter in data["letters"]:
        if letter.get("sent_at"):
            continue
        if (letter.get("deliver") or "") <= today_s:
            letter["sent_at"] = today_s
            changed = True
            just.append(letter.get("id"))
            if data.get("email"):
                send_mail_via_smtp(data.get("smtp") or {}, data["email"],
                                   letter.get("subject") or "一封信", letter.get("body") or "")

    bd = (data.get("birthday") or "").strip()
    if bd and bd == today_s[5:] and data.get("birthday_sent") != today_s[:4]:
        data["birthday_sent"] = today_s[:4]
        letter = {
            "id": "bd" + today_s.replace("-", ""),
            "created": today_s,
            "deliver": today_s,
            "subject": "生日快乐",
            "body": birthday_letter_body(data_dir, today),
            "sent_at": today_s,
            "read_at": "",
            "kind": "birthday",
        }
        data["letters"].insert(0, letter)
        changed = True
        just.append(letter["id"])
        if data.get("email"):
            send_mail_via_smtp(data.get("smtp") or {}, data["email"], letter["subject"], letter["body"])

    if changed:
        save_mail(data_dir, data)
    data["_just"] = just
    return data


def _norm_asset(src: str) -> str:
    s = str(src or "").strip().lstrip("./").replace("\\", "/")
    if s.startswith("assets/"):
        s = s[len("assets/"):]
    return s


def asset_refs(entries) -> set:
    refs = set()
    for e in entries:
        for p in (e.get("photos") or []):
            if isinstance(p, dict) and p.get("src"):
                refs.add(_norm_asset(p["src"]))
        imgs = e.get("images") or []
        if isinstance(imgs, list):
            for s in imgs:
                refs.add(_norm_asset(s))
        for m in re.findall(r"!\[[^\]]*\]\((https?://[^)]+|/assets/[^)]+)\)", e.get("body_md") or ""):
            if m.startswith("/assets/"):
                refs.add(_norm_asset(m[len("/assets/"):]))
    return {r for r in refs if r}


def gc_assets(data_dir: Path, dry: bool = False) -> list:
    """删掉 assets 里没有被任何一天引用的图片。"""
    refs = asset_refs(load_all(data_dir))
    base = data_dir / "assets"
    if not base.exists():
        return []
    removed = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if rel in refs:
            continue
        removed.append(rel)
        if not dry:
            try:
                path.unlink()
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------- AI 设置
SETTINGS_NAME = "settings.json"


def load_settings(data_dir: Path) -> dict:
    path = data_dir / SETTINGS_NAME
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("api_base", "https://api.deepseek.com/v1")
                data.setdefault("api_key", "")
                data.setdefault("model", "deepseek-chat")
                data.setdefault("ai_enabled", False)
                return data
        except Exception:
            pass
    return {"api_base": "https://api.deepseek.com/v1", "api_key": "",
            "model": "deepseek-chat", "ai_enabled": False}


def save_settings(data_dir: Path, data: dict) -> None:
    try:
        (data_dir / SETTINGS_NAME).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def period_entries(data_dir: Path, year: int, month: int = 0) -> list:
    out = []
    for e in load_all(data_dir):
        if e.get("sealed"):
            continue
        d = e["dateobj"]
        if d.year != year:
            continue
        if month and d.month != month:
            continue
        out.append(e)
    return out


def period_entries_range(data_dir: Path, start, end) -> list:
    """取 [start, end] 区间内的日记；上锁的不返回。start/end 为 date。"""
    out = []
    for e in load_all(data_dir):
        if e.get("sealed"):
            continue
        d = e["dateobj"]
        if start <= d <= end:
            out.append(e)
    return out


PROMPT_LIMIT = 120000


def summary_prompt(entries: list, label: str, period: str = "month") -> str:
    parts = []
    used = 0
    kept = 0
    with_body = [e for e in entries if (e.get("body_md") or "").strip()]
    for e in with_body:
        body = (e.get("body_md") or "").strip()
        piece = "【%s】\n%s" % (e["day"], body)
        if parts and used + len(piece) > PROMPT_LIMIT:
            break
        parts.append(piece)
        used += len(piece) + 2
        kept += 1
    text = "\n\n".join(parts)
    note = ""
    if kept < len(with_body):
        note = "（内容较长，只保留了前 %d 天，共 %d 天；要写更完整的回顾，可以分段生成。）\n\n" % (
            kept, len(with_body))
    focus = {
        "week": "这周每天在忙什么，哪几天情绪明显不一样",
        "month": "这个月的主线，哪些事反复出现、后来怎么收尾",
        "year": "这一年分成几个阶段，每段的重心是什么，前后有什么变化",
    }.get(period, "这段时间发生了什么")
    return (
        "以下是我 %s 的日记原文。请按下面的要求写一份回顾：\n"
        "1. 先用一段话说清 %s；\n"
        "2. 再按时间线列发生了什么，只陈述事实，不要评论；\n"
        "3. 然后说我反复想到的、在意的东西；\n"
        "4. 最后列出我提到但还没做的事；\n"
        "5. 用我的语气，短句，不要形容词堆砌，不要说「成长」「治愈」「情绪价值」这类词；\n"
        "6. 输出 Markdown，标题用「## %s 回顾」。\n\n"
        "日记：\n%s" % (label, focus, label, note + (text or "（这段时间没有记录）"))
    )


def report_name(period: str, start) -> str:
    """周报 2026-W43.md / 月报 2026-10.md / 年报 2026.md。"""
    if period == "week":
        y, w, _ = start.isocalendar()
        return "%d-W%02d.md" % (y, w)
    if period == "year":
        return "%d.md" % start.year
    return "%04d-%02d.md" % (start.year, start.month)


def save_report(data_dir: Path, period: str, start, text: str) -> str:
    """把回顾写进 data/reports/，返回相对路径。同名文件覆盖。"""
    folder = data_dir / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / report_name(period, start)
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(data_dir)).replace("\\", "/")


def list_reports(data_dir: Path) -> list:
    """列出 data/reports/ 里已有的回顾，新的在前。"""
    folder = data_dir / "reports"
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob("*.md"):
        name = path.name
        period, start, end = "", None, None
        m = re.match(r"^(\d{4})-W(\d{2})\.md$", name)
        if m:
            period = "week"
            try:
                start = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
                end = start + dt.timedelta(days=6)
            except ValueError:
                start = end = None
        else:
            m = re.match(r"^(\d{4})-(\d{2})\.md$", name)
            if m:
                period = "month"
                y, mo = int(m.group(1)), int(m.group(2))
                start = dt.date(y, mo, 1)
                end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            else:
                m = re.match(r"^(\d{4})\.md$", name)
                if not m:
                    continue
                period = "year"
                start = dt.date(int(m.group(1)), 1, 1)
                end = dt.date(int(m.group(1)), 12, 31)
        try:
            st = path.stat()
        except OSError:
            continue
        out.append({
            "name": name, "period": period,
            "start": start.isoformat() if start else "",
            "end": end.isoformat() if end else "",
            "size": st.st_size,
            "updated": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    out.sort(key=lambda x: (x["start"] or "", x["name"]), reverse=True)
    return out


def list_periods(data_dir: Path) -> list:
    """列出有日记的年月，新的在前，供回顾时直接选。"""
    base = data_dir / "days"
    if not base.is_dir():
        return []
    buckets = {}
    for path in base.rglob("*.md"):
        try:
            d = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        buckets.setdefault(d.year, {}).setdefault(d.month, 0)
        buckets[d.year][d.month] += 1
    out = []
    for year in sorted(buckets, reverse=True):
        months = [{"month": m, "days": n} for m, n in sorted(buckets[year].items(), reverse=True)]
        out.append({"year": year, "days": sum(buckets[year].values()), "months": months})
    return out


def read_report(data_dir: Path, name: str) -> str:
    """读一份回顾；name 只取文件名，防目录穿越。"""
    safe = Path(name or "").name
    if not safe.endswith(".md"):
        return ""
    path = data_dir / "reports" / safe
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def call_ai(settings: dict, prompt: str) -> str:
    import urllib.request
    key = (settings.get("api_key") or "").strip()
    if not key:
        return ""
    base = (settings.get("api_base") or "").rstrip("/")
    model = settings.get("model") or "deepseek-chat"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")


INDEX_NAME = "index.json"
INDEX_VERSION = 1


def _scan_days(data_dir: Path):
    out = []
    base = data_dir / "days"
    if not base.exists():
        return out
    for path in sorted(base.rglob("*.md")):
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path, st.st_mtime_ns, st.st_size))
    return out


def _load_index(data_dir: Path) -> dict:
    try:
        raw = json.loads((data_dir / INDEX_NAME).read_text(encoding="utf-8"))
        if raw.get("version") == INDEX_VERSION and isinstance(raw.get("files"), dict):
            return raw["files"]
    except Exception:
        pass
    return {}


def _save_index(data_dir: Path, cache: dict) -> None:
    try:
        (data_dir / INDEX_NAME).write_text(
            json.dumps({"version": INDEX_VERSION, "files": cache}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def load_all(data_dir: Path) -> list:
    """读全部日记。带一个按修改时间失效的索引：只重解析变过的文件。"""
    files = _scan_days(data_dir)
    if not files and not (data_dir / INDEX_NAME).exists():
        return []

    cache = _load_index(data_dir)
    entries = []
    seen = set()
    dirty = not cache

    for path, mtime, size in files:
        key = path.relative_to(data_dir).as_posix()
        seen.add(key)
        hit = cache.get(key)
        if hit and hit.get("mtime") == mtime and hit.get("size") == size and hit.get("entry"):
            item = dict(hit["entry"])
            try:
                item["dateobj"] = dt.date.fromisoformat(item["day"])
            except Exception:
                dirty = True
                continue
            entries.append(item)
            continue

        item = parse_entry(path)
        if not item:
            continue
        item["html"] = md_to_html(item["body_md"])
        entries.append(item)
        stored = {k: v for k, v in item.items() if k != "dateobj"}
        cache[key] = {"mtime": mtime, "size": size, "entry": stored}
        dirty = True

    stale = [k for k in cache if k not in seen]
    if stale:
        for k in stale:
            cache.pop(k, None)
        dirty = True

    if dirty:
        _save_index(data_dir, cache)

    entries.sort(key=lambda e: e["day"])
    return entries


def streaks(days: set, today: dt.date):
    """返回 (当前连续, 历史最长)."""
    best = 0
    for day in sorted(days):
        n = 1
        cur = day
        while (cur + dt.timedelta(days=1)) in days:
            cur += dt.timedelta(days=1)
            n += 1
        best = max(best, n)
    current = 0
    probe = today if today in days else today - dt.timedelta(days=1)
    while probe in days:
        current += 1
        probe -= dt.timedelta(days=1)
    return current, best


def hash_password(pw: str) -> str:
    return hashlib.sha256(("riji::" + pw).encode("utf-8")).hexdigest()


def check_password(entry: dict, pw: str) -> bool:
    stored = (entry or {}).get("lock_hash") or ""
    return bool(stored) and hmac.compare_digest(stored, hash_password(pw or ""))


def seal_entries(entries: list, today: dt.date) -> None:
    """上锁的和还没到日期的信，正文不出库。"""
    for e in entries:
        sealed = ""
        if e.get("lock_hash"):
            sealed = "lock"
        elif e.get("deliver"):
            try:
                d = dt.date.fromisoformat(e["deliver"])
            except ValueError:
                d = None
            if d and d > today:
                sealed = "letter"
        e["sealed"] = sealed
        if sealed:
            e["unlock"] = e.get("deliver") or ""
            e["body_md"] = ""
            e["html"] = ""
            e["excerpt"] = ""
            e["text"] = ""


def topic_digest(body_md: str) -> list:
    """[(名字, 该段第一行)]，只收起了名字的话题。"""
    out = []
    cur = None
    idx = 0
    for line in str(body_md or "").split("\n"):
        if line.startswith("## "):
            idx += 1
            head = line[3:]
            tags = re.findall(r"#([^\s#]+)", head)
            name = re.sub(r"#[^\s#]+", "", head).strip()
            auto = (not name) or bool(re.match(r"^话题\s*\d+$", name))
            # 起了名字的、或者带标签的段落，都算“有话题”
            if auto and not tags:
                cur = None
                continue
            cur = {"name": "" if auto else name, "n": idx,
                   "tags": tags, "lines": []}
            out.append(cur)
            continue
        if cur is not None:
            cur["lines"].append(line)
    for t in out:
        preview = re.sub(r"\s+", " ", " ".join(t["lines"])).strip()
        t["preview"] = preview[:80]
        t["label"] = t["name"] or ("话题 %d" % t["n"])
        del t["lines"]
    return out


def tags_of(body_md: str, extra) -> list:
    seen = []
    def add(x):
        x = str(x or "").strip()
        if x and x not in seen:
            seen.append(x)
    if isinstance(extra, list):
        for t in extra:
            add(t)
    elif isinstance(extra, str) and extra:
        for t in re.split(r"[,，\s]+", extra):
            add(t)
    for m in re.findall(r"##[^\n]*?#([^\s#]+)", str(body_md or "")):
        add(m)
    return seen


def tags_root(data_dir: Path) -> Path:
    """标签快照目录：data/stats/tags/。每份都是一个时间点的纯文本记录。"""
    return Path(data_dir) / "stats" / "tags"


def tag_stats(data_dir: Path) -> dict:
    """按标签跨时间聚合：多少天写过、最早/最晚、每年各几次。"""
    entries = load_all(data_dir)
    hits = {}
    for e in entries:
        for t in tags_of(e.get("body_md"), e.get("tags")):
            hits.setdefault(t, set()).add(e["day"])
    items = []
    for name, ds in hits.items():
        days = sorted(ds)
        years = {}
        for d in days:
            years[d[:4]] = years.get(d[:4], 0) + 1
        items.append({
            "name": name,
            "days": len(days),
            "first": days[0],
            "last": days[-1],
            "years": years,
        })
    items.sort(key=lambda x: (-x["days"], x["first"]))
    return {
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "items": items,
    }


def tag_snapshots(data_dir: Path) -> list:
    """历史快照清单（不含明细）。"""
    root = tags_root(data_dir)
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.glob("tags_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "file": p.name,
            "at": d.get("at", ""),
            "total": d.get("total", 0),
            "added": d.get("added", []),
            "gone": d.get("gone", []),
        })
    return out


def read_tag_snapshot(data_dir: Path, name: str = "") -> dict:
    root = tags_root(data_dir)
    files = sorted(root.glob("tags_*.json")) if root.is_dir() else []
    if not files:
        return {}
    pick = root / Path(name).name if name else files[-1]
    if not str(pick.resolve()).startswith(str(root.resolve())) or not pick.is_file():
        pick = files[-1]
    try:
        return json.loads(pick.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_tag_snapshot(data_dir: Path, stats: dict = None) -> dict:
    """算一次并落一份快照。标签集合没变就不写，避免刷出一堆重复文件。"""
    stats = stats or tag_stats(data_dir)
    root = tags_root(data_dir)
    files = sorted(root.glob("tags_*.json")) if root.is_dir() else []
    prev = None
    if files:
        try:
            prev = json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception:
            prev = None
    names = [t["name"] for t in stats["items"]]
    prev_names = [t.get("name", "") for t in (prev or {}).get("items", [])]
    if prev is not None and names == prev_names:
        stats["added"] = []
        stats["gone"] = []
        stats["file"] = files[-1].name
        stats["unchanged"] = True
        return stats
    old = set(prev_names)
    now = set(names)
    stats["added"] = [n for n in names if n not in old]
    stats["gone"] = [n for n in prev_names if n not in now]
    stats["file"] = dt.datetime.now().strftime("tags_%Y%m%d_%H%M%S.json")
    stats["unchanged"] = False
    root.mkdir(parents=True, exist_ok=True)
    stem = stats["file"][:-5]
    n = 2
    while (root / stats["file"]).exists():
        stats["file"] = "%s_%d.json" % (stem, n)
        n += 1
    (root / stats["file"]).write_text(
        json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def onthisday(data_dir: Path, today: dt.date = None, span: int = 3) -> list:
    """往年同月同日，最近 3 年。纯本地，2 月 29 日只在闰年出现。"""
    today = today or dt.date.today()
    out = []
    for y in range(today.year - span, today.year):
        try:
            d = dt.date(y, today.month, today.day)
        except ValueError:
            continue
        p = day_path(data_dir, d)
        if not p.is_file():
            continue
        e = parse_entry(p)
        if not e:
            continue
        sealed = bool(e.get("sealed"))
        out.append({
            "date": e["day"],
            "years": today.year - y,
            "words": 0 if sealed else int(e.get("words") or 0),
            "excerpt": "" if sealed else (e.get("excerpt") or ""),
            "tags": [] if sealed else tags_of(e.get("body_md"), e.get("tags")),
            "sealed": sealed,
        })
    return sorted(out, key=lambda x: x["date"], reverse=True)


def _grams(text: str) -> set:
    """中文不分词，用 2-gram 就够：标点空格全丢掉，相邻两字成一组。"""
    s = re.sub(r"[\s\W_]+", "", str(text or "").lower())
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def search_units(entries) -> list:
    """按 ## 话题切段，每段单独算分。只有正文的日子整篇算一段。"""
    units = []
    for e in entries:
        if e.get("sealed"):
            continue
        body = e.get("body_md") or ""
        if not body.strip():
            continue
        chunks = re.split(r"(?m)^##", body)
        for ch in chunks:
            text = ch.strip()
            if not text:
                continue
            lines = text.split("\n")
            head = lines[0].strip()
            label = re.sub(r"#[^\s#]+", "", head).strip()[:40] if head else ""
            if re.match(r"^话题\s*\d+$", label):
                label = ""
            body_text = "\n".join(lines[1:]).strip() if label else text
            units.append({
                "date": e["day"],
                "label": label,
                "text": (body_text or text)[:4000],
                "grams": _grams(text),
            })
    return units


def search_entries(data_dir: Path, keywords, mode: str = "any", limit: int = 20) -> list:
    """本地检索。mode=any 按相关度，mode=first 取最早命中的那一天。"""
    kws = []
    for k in keywords or []:
        k = str(k or "").strip()
        if k and k not in kws:
            kws.append(k)
    if not kws:
        return []
    units = search_units(load_all(data_dir))
    scored = []
    for u in units:
        hit, score = [], 0.0
        for k in kws:
            g = _grams(k)
            if not g:
                continue
            ov = len(g & u["grams"])
            if not ov:
                continue
            hit.append(k)
            score += ov / float(len(g))
        if hit:
            scored.append({"hit": hit, "score": round(score, 3), "unit": u})
    if not scored:
        return []

    def snippet(u, kw):
        t = u["text"]
        pos = -1
        for k in kw:
            pos = t.find(k)
            if pos >= 0:
                break
        if pos < 0:
            return t[:80]
        a = max(0, pos - 30)
        return ("…" if a else "") + t[a:pos + 60] + ("…" if pos + 60 < len(t) else "")

    if mode == "first":

        early = min(x["unit"]["date"] for x in scored)
        picked = [x for x in scored if x["unit"]["date"] == early]
        picked.sort(key=lambda x: -x["score"])
    else:
        scored.sort(key=lambda x: (-len(x["hit"]), -x["score"], x["unit"]["date"]))
        picked = scored[:limit]

    out = []
    for x in picked[:limit]:
        u = x["unit"]
        out.append({
            "date": u["date"],
            "label": u["label"],
            "snippet": re.sub(r"\s+", " ", snippet(u, x["hit"])).strip(),
            "hit": x["hit"],
            "score": x["score"],
        })
    return out


def entry_digest(e: dict) -> dict:
    """列表/日历用的轻量条目：不带正文。"""
    body = e.get("body_md") or ""
    return {
        "date": e["day"],
        "title": e.get("title") or "",
        "mood": e.get("mood") or "",
        "weather": e.get("weather") or "",
        "words": e.get("words") or 0,
        "excerpt": e.get("excerpt") or "",
        "sealed": e.get("sealed") or "",
        "lock_hint": e.get("lock_hint") or "",
        "unlock": e.get("unlock") or "",
        "updated": e.get("updated") or "",
        "color": e.get("color") or "",
        "created": e.get("created") or "",
        "has": bool(str(body).strip() or (e.get("photos") or []) or (e.get("strokes") or [])),
        "photos": len(e.get("photos") or []),
        "tops": topic_digest(body),
        "tags": tags_of(body, e.get("tags")),
    }


def build_payload(data_dir: Path, title: str = "日迹") -> dict:
    entries = load_all(data_dir)
    days = {e["dateobj"] for e in entries}
    today = dt.date.today()
    seal_entries(entries, today)
    current, best = streaks(days, today)
    months = sorted({e["day"][:7] for e in entries})
    years = sorted({e["day"][:4] for e in entries}) or [str(today.year)]
    return {
        "title": title,
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "data_dir": str(data_dir),
        "birthday": (load_settings(data_dir) or {}).get("birthday", ""),
        "entries": [entry_digest(e) for e in entries],
        "months": months,
        "years": years,
        "stats": {
            "total": len(days),
            "words": sum(int(e["words"]) for e in entries),
            "current": current,
            "best": best,
            "first": entries[0]["day"] if entries else "",
        },
    }


# ---------------------------------------------------------------- 命令

EXTRAS_TEXT = ("lock_hash", "lock_hint", "deliver", "images", "color", "author")
EXTRAS_JSON = ("comments", "photos", "topic_ids", "topic_times", "strokes")


def extras_of(old):
    """把旧条目的 frontmatter 原样带回去 —— 追加不能吞掉留言/图片/话题 id。"""
    extra = {}
    for k in EXTRAS_TEXT:
        if old.get(k):
            extra[k] = old[k]
    for k in EXTRAS_JSON:
        raw = old.get(k)
        if raw:
            try:
                extra[k] = json.loads(raw)
            except Exception:
                pass
    return extra


def cmd_add(args) -> int:
    data_dir = Path(args.data)
    day = parse_date(args.date)
    path = day_path(data_dir, day)
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")
    if not body.strip() and not sys.stdin.isatty():
        body = sys.stdin.read()
    if not body.strip():
        print("ERROR: 正文为空, 拒绝创建空日记。请用 --body 或 --body-file 提供内容。")
        return 2

    created = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    extra = {"author": "whale"}
    if path.exists() and args.append:
        old = parse_entry(path)
        if old:
            if old["body_md"]:
                body = old["body_md"] + "\n\n" + body.strip()
            args.mood = args.mood or old["mood"]
            args.weather = args.weather or old["weather"]
            args.title = args.title or old["title"]
            if not args.tags:
                args.tags = [t for t in old["tags"].split(",") if t.strip()]
            if old.get("created"):
                created = old["created"]
            extra = extras_of(old)

    path.parent.mkdir(parents=True, exist_ok=True)
    text = dump_entry(day, args.mood or "", args.weather or "", args.tags or [],
                      args.title or "", body, created, extra=extra)
    path.write_text(text, encoding="utf-8")

    verb = "已追加" if args.append else "已写入"
    print("%s %s -> %s" % (verb, day.isoformat(), path))
    print("   %s · %s · %d 字" % (args.mood or "未记录心情", args.weather or "未记录天气",
                                 count_words(strip_md(body))))
    return 0


def cmd_build(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from render import build_html

    data_dir = Path(args.data)
    out = Path(args.out) if args.out else data_dir.parent / "index.html"
    payload = build_payload(data_dir, args.title)
    if not payload["entries"]:
        print("WARN: 还没有任何日记, 先生成一张空白日历。")
    html = build_html(payload)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("已生成日历: %s (%d 天, %.1f KB)" % (out, payload["stats"]["total"],
                                              len(html) / 1024))
    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_list(args) -> int:
    data_dir = Path(args.data)
    entries = load_all(data_dir)
    if not entries:
        print("(还没有日记)")
        return 0
    show = entries[-args.limit:] if args.limit else entries
    for e in show:
        print("%s  %-6s %-4s %s" % (e["day"], e["mood"] or "-", e["weather"] or "-",
                                    e["title"] or e["excerpt"]))
    print("共 %d 天" % len(entries))
    return 0


def cmd_stats(args) -> int:
    data_dir = Path(args.data)
    payload = build_payload(data_dir)
    s = payload["stats"]
    if not s["total"]:
        print("(还没有日记)")
        return 0
    print("总天数   %d 天" % s["total"])
    print("总字数   %d 字" % s["words"])
    print("当前连续 %d 天" % s["current"])
    print("最长连续 %d 天" % s["best"])
    print("第一页   %s" % s["first"])
    moods = {}
    for e in payload["entries"]:
        if e["mood"]:
            moods[e["mood"]] = moods.get(e["mood"], 0) + 1
    if moods:
        top = sorted(moods.items(), key=lambda kv: -kv[1])[:5]
        print("心情     " + "  ".join("%s×%d" % (k, v) for k, v in top))
    return 0


def cmd_export(args) -> int:
    data_dir = Path(args.data)
    entries = load_all(data_dir)
    if args.year:
        entries = [e for e in entries if e["day"].startswith(str(args.year))]
    if not entries:
        print("(没有可导出的日记)")
        return 1
    out = Path(args.out) if args.out else data_dir.parent / ("riji-%s.md" % (args.year or "all"))
    lines = ["# 日迹 · %s" % (args.year or "全部"), ""]
    current_month = ""
    for e in entries:
        month = e["day"][:7]
        if month != current_month:
            current_month = month
            lines.append("\n## %s 年 %s 月\n" % (month[:4], month[5:7]))
        lines.append("### %s  %s" % (e["day"], e["title"]))
        if e["mood"] or e["weather"]:
            lines.append("*%s · %s*" % (e["mood"] or "", e["weather"] or ""))
        lines.append("")
        lines.append(e["body_md"])
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    print("已导出 %d 天 -> %s" % (len(entries), out))
    return 0


def main(argv=None) -> int:
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(prog="riji", description="日迹 · 一天一格的一页式日记")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="数据目录 (默认 skill 下的 data/)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="写入/追加某一天的日记")
    p_add.add_argument("--date", default="today")
    p_add.add_argument("--mood", default="")
    p_add.add_argument("--weather", default="")
    p_add.add_argument("--tags", nargs="*", default=[])
    p_add.add_argument("--title", default="")
    p_add.add_argument("--body", default="")
    p_add.add_argument("--body-file", default="")
    p_add.add_argument("--append", action="store_true", help="在已有当天日记后追加")
    p_add.set_defaults(func=cmd_add)

    p_build = sub.add_parser("build", help="生成日历 HTML")
    p_build.add_argument("--out", default="")
    p_build.add_argument("--title", default="日迹")
    p_build.add_argument("--open", action="store_true")
    p_build.set_defaults(func=cmd_build)

    p_list = sub.add_parser("list", help="列出已写的日子")
    p_list.add_argument("--limit", type=int, default=0)
    p_list.set_defaults(func=cmd_list)

    p_stats = sub.add_parser("stats", help="统计")
    p_stats.set_defaults(func=cmd_stats)

    p_export = sub.add_parser("export", help="导出为长文档 (年度回顾素材)")
    p_export.add_argument("--year", default="")
    p_export.add_argument("--out", default="")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
