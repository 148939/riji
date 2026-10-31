# -*- coding: utf-8 -*-
"""账号：只有云端版用。一个邮箱一个账号，token 长期有效（登一次记住）。

密码用 pbkdf2-sha256 + 随机盐；数据库里只存 token 的 sha256，泄库也换不出能用的 token。
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

ITER = 200_000
TOKEN_DAYS = 90


def _connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(db_path):
    conn = _connect(db_path)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        pw_salt TEXT NOT NULL,
        pw_hash TEXT NOT NULL,
        created TEXT NOT NULL,
        last_login TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created REAL NOT NULL,
        expires REAL NOT NULL,
        seen REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE TABLE IF NOT EXISTS codes (
        email TEXT PRIMARY KEY,
        code_hash TEXT NOT NULL,
        expires REAL NOT NULL,
        created REAL NOT NULL,
        tries INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS resets (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        expires REAL NOT NULL
    );
    """)
    conn.commit()
    conn.close()


def _hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), ITER).hex()


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _norm_email(email):
    return (email or "").strip().lower()


def register(db_path, email, password):
    email = _norm_email(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return None, "邮箱格式不对"
    if len(password or "") < 6:
        return None, "密码至少 6 位"
    salt = os.urandom(16).hex()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO users (email, pw_salt, pw_hash, created) VALUES (?,?,?,?)",
            (email, salt, _hash_pw(password, salt), now))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
    except sqlite3.IntegrityError:
        conn.close()
        return None, "这个邮箱已经注册过了"
    conn.close()
    return uid, ""


def login(db_path, email, password):
    email = _norm_email(email)
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        conn.close()
        return None, "邮箱或密码不对"
    if not hmac.compare_digest(row["pw_hash"], _hash_pw(password or "", row["pw_salt"])):
        conn.close()
        return None, "邮箱或密码不对"
    conn.execute("UPDATE users SET last_login=? WHERE id=?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), row["id"]))
    conn.commit()
    conn.close()
    return row["id"], ""


def new_token(db_path, uid):
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect(db_path)
    conn.execute("INSERT INTO sessions (token_hash, user_id, created, expires, seen) VALUES (?,?,?,?,?)",
                 (_hash_token(token), uid, now, now + TOKEN_DAYS * 86400, now))
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.commit()
    conn.close()
    return token


def user_by_token(db_path, token):
    """返回 (uid, email)；每次访问顺延有效期。"""
    if not token:
        return None
    now = time.time()
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT s.user_id, s.expires, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash=?", (_hash_token(token),)).fetchone()
    if not row or row["expires"] < now:
        conn.close()
        return None
    # 每次使用往后延 90 天，长期不用才失效
    if row["expires"] - now < (TOKEN_DAYS - 7) * 86400:
        conn.execute("UPDATE sessions SET expires=?, seen=? WHERE token_hash=?",
                     (now + TOKEN_DAYS * 86400, now, _hash_token(token)))
        conn.commit()
    else:
        conn.execute("UPDATE sessions SET seen=? WHERE token_hash=?", (now, _hash_token(token)))
        conn.commit()
    conn.close()
    return row["user_id"], row["email"]


CODE_TTL = 600      # 10 分钟
CODE_GAP = 60       # 同一邮箱 60 秒只能要一次


def request_code(db_path, email):
    """给这个邮箱发注册验证码。返回 (code, error)。"""
    email = _norm_email(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        return "", "邮箱格式不对"
    now = time.time()
    conn = _connect(db_path)
    if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        conn.close()
        return "", "这个邮箱已经注册过了"
    row = conn.execute("SELECT created FROM codes WHERE email=?", (email,)).fetchone()
    if row and now - row["created"] < CODE_GAP:
        conn.close()
        return "", "刚发过，等一分钟再试"
    code = "%06d" % secrets.randbelow(1000000)
    conn.execute("INSERT OR REPLACE INTO codes (email, code_hash, expires, created, tries) "
                 "VALUES (?,?,?,?,0)",
                 (email, _hash_token(code), now + CODE_TTL, now))
    conn.execute("DELETE FROM codes WHERE expires < ?", (now,))
    conn.commit()
    conn.close()
    return code, ""


def check_code(db_path, email, code):
    """验证码对不对。对了就作废。"""
    email = _norm_email(email)
    code = str(code or "").strip()
    now = time.time()
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM codes WHERE email=?", (email,)).fetchone()
    if not row or row["expires"] < now:
        conn.close()
        return False, "验证码过期了，重新发一次"
    if row["tries"] >= 6:
        conn.execute("DELETE FROM codes WHERE email=?", (email,))
        conn.commit()
        conn.close()
        return False, "错太多次了，重新发一次"
    conn.execute("UPDATE codes SET tries = tries + 1 WHERE email=?", (email,))
    conn.commit()
    if not hmac.compare_digest(row["code_hash"], _hash_token(code)):
        conn.close()
        return False, "验证码不对"
    conn.execute("DELETE FROM codes WHERE email=?", (email,))
    conn.commit()
    conn.close()
    return True, ""


def make_reset(db_path, email, hours=2):
    """生成重置口令。返回 (user_id, 明文 token) 或 (None, '')。"""
    email = _norm_email(email)
    conn = _connect(db_path)
    row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        conn.close()
        return None, ""
    token = secrets.token_urlsafe(24)
    now = time.time()
    conn.execute("DELETE FROM resets WHERE expires < ?", (now,))
    conn.execute("INSERT INTO resets (token_hash, user_id, expires) VALUES (?,?,?)",
                 (_hash_token(token), row["id"], now + hours * 3600))
    conn.commit()
    conn.close()
    return row["id"], token


def reset_password(db_path, token, password):
    if len(password or "") < 6:
        return None, "密码至少 6 位"
    conn = _connect(db_path)
    now = time.time()
    row = conn.execute("SELECT user_id FROM resets WHERE token_hash=? AND expires>?",
                       (_hash_token(token), now)).fetchone()
    if not row:
        conn.close()
        return None, "链接已失效，请重新申请"
    uid = row["user_id"]
    salt = os.urandom(16).hex()
    conn.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?",
                 (salt, _hash_pw(password, salt), uid))
    conn.execute("DELETE FROM resets WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))  # 所有旧登录作废
    conn.commit()
    conn.close()
    return uid, ""


def delete_account(db_path, uid):
    conn = _connect(db_path)
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM resets WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()


def check_password(db_path, uid, password):
    conn = _connect(db_path)
    row = conn.execute("SELECT pw_salt, pw_hash FROM users WHERE id=?", (uid,)).fetchone()
    ok = bool(row) and hmac.compare_digest(row["pw_hash"], _hash_pw(password or "", row["pw_salt"]))
    conn.close()
    return ok


def logout(db_path, token):
    if not token:
        return
    conn = _connect(db_path)
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))
    conn.commit()
    conn.close()
