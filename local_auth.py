"""Offline accounts. Only password hashes are persisted in the account database."""
from contextlib import closing
from hashlib import pbkdf2_hmac
import hmac
from http.cookies import SimpleCookie, CookieError
import re
import secrets
import sqlite3
import time

ROLES = ('admin', 'editor', 'reviewer', 'viewer')
LIFETIME = 8 * 60 * 60


class Accounts:
    def __init__(self, path):
        self.path = path
        self.cookie = 'GHG_' + secrets.token_hex(8)
        self.sessions = {}
        self.login_after = 0

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute("""CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY COLLATE NOCASE, role TEXT NOT NULL,
            salt BLOB NOT NULL, digest BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""")
        if 'enabled' not in {row[1] for row in db.execute('PRAGMA table_info(users)')}:
            db.execute('ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
        return db

    def empty(self):
        with closing(self.connect()) as db:
            return db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None

    @staticmethod
    def digest(password, salt):
        return pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600_000)

    def add(self, name, password, role, first=False):
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', name)
                or not isinstance(password, str) or not 12 <= len(password) <= 128
                or role not in ROLES):
            raise ValueError('帳號需為 3 至 40 個英數字或 . _ -，密碼需為 12 至 128 字元。')
        salt = secrets.token_bytes(16)
        try:
            with closing(self.connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                if first and db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                    raise ValueError('已建立管理員。')
                db.execute('INSERT INTO users (name, role, salt, digest) VALUES (?, ?, ?, ?)',
                           (name, role, salt, self.digest(password, salt)))
        except sqlite3.IntegrityError as exc:
            raise ValueError('帳號已存在。') from exc

    def login(self, name, password):
        now = time.monotonic()
        if now < self.login_after:
            return None
        self.login_after = now + 1
        if not isinstance(name, str) or not isinstance(password, str) or len(password) > 128:
            return None
        with closing(self.connect()) as db:
            row = db.execute('SELECT name, role, salt, digest FROM users WHERE name=? AND enabled=1', (name,)).fetchone()
        test = self.digest(password, row[2] if row else b'\0' * 16)
        if not row or not hmac.compare_digest(test, row[3]):
            return None
        self.sessions = {key: value for key, value in self.sessions.items() if value[2] > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = (row[0], row[1], now + LIFETIME)
        self.login_after = 0
        return token

    def list_users(self):
        with closing(self.connect()) as db:
            return [{'name': name, 'role': role, 'enabled': bool(enabled)}
                    for name, role, enabled in db.execute('SELECT name, role, enabled FROM users ORDER BY name')]

    def update(self, actor, name, action, password):
        if not isinstance(name, str):
            raise ValueError('請選擇帳號。')
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT name FROM users WHERE name=?', (name,)).fetchone()
            if not row:
                raise ValueError('找不到指定帳號。')
            name = row[0]
            if action == 'reset':
                if not isinstance(password, str) or not 12 <= len(password) <= 128:
                    raise ValueError('密碼需為 12 至 128 字元。')
                salt = secrets.token_bytes(16)
                db.execute('UPDATE users SET salt=?, digest=? WHERE name=?',
                           (salt, self.digest(password, salt), name))
            elif action in ('enable', 'disable') and actor != name:
                db.execute('UPDATE users SET enabled=? WHERE name=?', (int(action == 'enable'), name))
            else:
                raise ValueError('無法停用目前登入的管理員。')
        self.sessions = {key: value for key, value in self.sessions.items() if value[0] != name}

    def session_key(self, header):
        try:
            cookies = SimpleCookie(header or '')
        except CookieError:
            return None
        cookie = cookies.get(self.cookie)
        return cookie.value if cookie else None

    def identify(self, header):
        session = self.sessions.get(self.session_key(header))
        return session[:2] if session and session[2] > time.monotonic() else None
