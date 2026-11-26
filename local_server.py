"""GHG V2 local storage prototype. Python 3.10+, standard library only."""
import json
import base64
import binascii
import hashlib
import time
import re
import os
import sqlite3
import secrets
import webbrowser
import xlsx_local
import inventory_quality
from annual_report import annual_report
from local_auth import Accounts, LIFETIME
from inventory_years import annual_totals, validate_year_settings
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GHGSystem"
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "inventory.sqlite3"
TOKEN = secrets.token_urlsafe(32)
ACCOUNTS = Accounts(DATA / "accounts.sqlite3")
BACKUPS = DATA / "backups"
KEYS = ("sources", "factors", "records", "evidence", "audit")
OPTIONAL_KEYS = ("assessments", "yearSettings")

def connect():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS evidence_files (id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS change_history (id INTEGER PRIMARY KEY AUTOINCREMENT, changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), operation TEXT NOT NULL, collection TEXT NOT NULL, item_id TEXT NOT NULL, before_json TEXT, after_json TEXT, reason TEXT NOT NULL DEFAULT '')")
    if "actor" not in {row[1] for row in con.execute("PRAGMA table_info(change_history)")}:
        con.execute("ALTER TABLE change_history ADD COLUMN actor TEXT")
    return con

def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        raise ValueError("Invalid inventory settings")
    for key in KEYS:
        if not isinstance(data.get(key), list) or any(not isinstance(x, dict) for x in data[key]):
            raise ValueError("Invalid inventory collection: " + key)
    for key in OPTIONAL_KEYS:
        if key in data and (not isinstance(data[key], list) or any(not isinstance(x, dict) for x in data[key])):
            raise ValueError("Invalid optional collection: " + key)
    validate_year_settings(data.get("yearSettings", []))
    return json.dumps(data, ensure_ascii=False, allow_nan=False)

def revision(payload):
    return hashlib.sha256((payload if payload is not None else "null").encode("utf-8")).hexdigest()


def protect_reviewed(previous, payload):
    if previous is None:
        return
    old, new = json.loads(previous), json.loads(payload)
    updated = {}
    for record in new["records"]:
        key = record.get("id")
        if isinstance(key, str):
            if key in updated:
                raise ValueError("重複紀錄編號")
            updated[key] = record
    for record in old["records"]:
        if record.get("status") != "已審核":
            continue
        replacement = updated.get(record.get("id"))
        if replacement == record:
            continue
        returned = dict(record)
        returned["status"] = "退回"
        if replacement != returned:
            raise ValueError("已審核的紀錄需先退回，才能修改或刪除")


def review_permissions(previous, payload, role):
    old = json.loads(previous) if previous else {"records": []}
    new = json.loads(payload)
    if role == "reviewer":
        if previous is None:
            return False
        for key in OPTIONAL_KEYS:
            old.setdefault(key, [])
            new.setdefault(key, [])
        if {k: v for k, v in old.items() if k != "records"} != {k: v for k, v in new.items() if k != "records"}:
            return False
        if len(old["records"]) != len(new["records"]):
            return False
        for before, after in zip(old["records"], new["records"]):
            if before == after:
                continue
            allowed = {"待審核": ("已審核", "退回"), "已審核": ("退回",)}
            status = before.get("status")
            target = after.get("status")
            if not isinstance(status, str) or target not in allowed.get(status, ()):
                return False
            if after != {**before, "status": target}:
                return False
        return True
    reviewed = {item.get("id"): item for item in old["records"]
                if isinstance(item.get("id"), str) and item.get("status") in ("已審核", "退回")}
    for item in new["records"]:
        if item.get("status") in ("已審核", "退回"):
            prior = reviewed.get(item.get("id")) if isinstance(item.get("id"), str) else None
            if not prior or prior.get("status") != item.get("status"):
                return False
    return True


def change_events(previous, payload, reason):
    """Produce explicit before/after records without asserting a user identity."""
    old = json.loads(previous) if previous else {"settings": {}, **{k: [] for k in KEYS}}
    new = json.loads(payload)
    events = []
    def add(operation, collection, item_id, before, after):
        events.append((operation, collection, str(item_id),
                       json.dumps(before, ensure_ascii=False) if before is not None else None,
                       json.dumps(after, ensure_ascii=False) if after is not None else None, reason))
    if old.get("settings") != new.get("settings"):
        add("修改", "settings", "settings", old.get("settings"), new.get("settings"))
    for collection in KEYS + OPTIONAL_KEYS:
        before_list, after_list = old.get(collection, []), new.get(collection, [])
        def index(items):
            mapping = {}
            for item in items:
                key = item.get("id")
                if not isinstance(key, str) or not key or key in mapping:
                    return None
                mapping[key] = item
            return mapping
        before, after = index(before_list), index(after_list)
        if before is None or after is None:
            if before_list != after_list:
                add("整批變更", collection, "(清單)", before_list, after_list)
            continue
        for key in sorted(before.keys() | after.keys()):
            if key not in before: add("新增", collection, key, None, after[key])
            elif key not in after: add("刪除", collection, key, before[key], None)
            elif before[key] != after[key]: add("修改", collection, key, before[key], after[key])
    return events


def write_history(con, previous, payload, reason, actor):
    events = change_events(previous, payload, reason)
    if not events and reason == "備份還原":
        events = [("還原", "inventory", "(全部)", None, None, reason)]
    con.executemany("INSERT INTO change_history(operation,collection,item_id,before_json,after_json,reason,actor) VALUES(?,?,?,?,?,?,?)",
                    [(*event, actor) for event in events])


def backup_database(con):
    """Keep a SQLite-consistent copy before each replacement of existing data."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    dest = BACKUPS / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4) + ".sqlite3")
    try:
        with sqlite3.connect(dest) as target:
            con.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Backup integrity check failed")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    backups = sorted(BACKUPS.glob("*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[30:]:
        try:
            old.unlink()
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body, mime="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if hasattr(self, "response_revision"):
            self.send_header("X-GHG-Revision", self.response_revision)
        if hasattr(self, "session_cookie"):
            self.send_header("Set-Cookie", self.session_cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        return self.headers.get("Host") == "127.0.0.1:" + str(self.server.server_port)

    def backup_names(self):
        if not BACKUPS.exists():
            return []
        pattern = re.compile(r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.sqlite3")
        return sorted((p.name for p in BACKUPS.iterdir() if pattern.fullmatch(p.name) and p.is_file() and not p.is_symlink()), reverse=True)

    def json_reply(self, status, data):
        return self.reply(status, json.dumps(data, ensure_ascii=False).encode())

    def identified(self):
        return ACCOUNTS.identify(self.headers.get("Cookie"))

    def auth_page(self):
        template = (ROOT / "login.html").read_text(encoding="utf-8")
        return self.reply(200, template.replace("__TOKEN__", TOKEN).replace(
            "__SETUP__", "true" if ACCOUNTS.empty() else "false").encode(),
            "text/html; charset=utf-8")

    def auth_action(self):
        path = self.path
        account = self.identified()
        if path == "/api/logout":
            ACCOUNTS.sessions.pop(ACCOUNTS.session_key(self.headers.get("Cookie")), None)
            self.session_cookie = f"{ACCOUNTS.cookie}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            return self.json_reply(200, {})
        if path in ("/api/users", "/api/users/update") and (not account or account[1] != "admin"):
            return self.json_reply(403, {"error": "此功能僅開放給管理員。"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("帳號資料格式不正確。")
            fields = json.loads(self.rfile.read(size))
            if not isinstance(fields, dict):
                raise ValueError("帳號資料格式不正確。")
            name, password = fields.get("name"), fields.get("password")
            if path == "/api/users/update":
                ACCOUNTS.update(account[0], name, fields.get("action"), password)
                return self.json_reply(200, {})
            if path in ("/api/setup", "/api/users"):
                first = path == "/api/setup"
                if first and not ACCOUNTS.empty():
                    return self.json_reply(409, {"error": "已建立管理員。"})
                ACCOUNTS.add(name, password, "admin" if first else fields.get("role"), first=first)
                return self.json_reply(201, {})
            token = ACCOUNTS.login(name, password)
            if not token:
                return self.json_reply(401, {"error": "登入失敗，請確認帳密後稍候再試。"})
            self.session_cookie = f"{ACCOUNTS.cookie}={token}; Path=/; Max-Age={LIFETIME}; HttpOnly; SameSite=Strict"
            return self.json_reply(200, {})
        except (ValueError, UnicodeError, RecursionError) as error:
            return self.json_reply(400, {"error": str(error)})
        except (sqlite3.Error, OSError):
            return self.json_reply(500, {"error": "無法讀寫本機帳號資料庫。"})

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, b"{}")
        account = self.identified()
        if self.path == "/" and not account:
            return self.auth_page()
        if self.path != "/" and (not account or self.headers.get("X-GHG-Token") != TOKEN):
            return self.reply(404, b"{}")
        if self.path == "/":
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = """<script>
const localToken = "__TOKEN__";
const localAccount = __ACCOUNT__;
let localRevision = null;
function localRequest(method, data) {
  const req = new XMLHttpRequest();
  req.open(method, "/api/inventory", false);
  req.setRequestHeader("X-GHG-Token", localToken);
  if (method === "PUT") { req.setRequestHeader("Content-Type", "application/json"); req.setRequestHeader("X-GHG-Revision", localRevision || ""); }
  req.send(data || null);
  if (req.status === 409) { alert("資料已由其他視窗變更，本次未儲存。請重新整理後再操作。"); throw Error("資料版本衝突，本次未儲存"); }
  if (req.status === 403) { alert("此帳號無權執行這項操作，或登入已逾時。"); throw Error("權限不足"); }
  if (req.status !== 200) { alert("資料儲存或讀取失敗，本次變更可能未儲存。請保留畫面並聯絡管理人員。"); throw Error("Local database request failed: " + req.status); }
  localRevision = req.getResponseHeader("X-GHG-Revision");
  return req.responseText;
}
const localStore = {
  getItem: () => { const raw = localRequest("GET"); return raw === "null" ? null : raw; },
  setItem: (key, value) => { localRequest("PUT", value); }
};
</script>""".replace("__TOKEN__", TOKEN).replace("__ACCOUNT__", json.dumps(account))
            html = html.replace('<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>', "")
            html = html.replace("Excel 功能載入失敗，請確認網路連線後重新整理。", "離線預覽版尚未提供 Excel 功能，請使用 CSV 或 JSON 備份。")
            html = html.replace("<script>", bridge + "<script>", 1).replace("localStorage.getItem", "localStore.getItem").replace("localStorage.setItem", "localStore.setItem")
            html = html.replace("企業溫室氣體盤查管理系統 V1.0", "企業溫室氣體盤查管理系統 V2.0 本機預覽版")
            html = html.replace("本版資料儲存在此瀏覽器的 localStorage，尚未提供登入、多人同步、真正檔案儲存或雲端備份。", "本機預覽版使用 SQLite 儲存。請定期建立備份。")
            html = html.replace("目前仍使用本機瀏覽器儲存", "目前使用本機 SQLite 儲存")
            html = html.replace("catch{db=fresh()}", "catch(e){document.body.textContent='資料庫讀取失敗，請勿繼續操作。';throw e}")
            return self.reply(200, html.encode(), "text/html; charset=utf-8")
        if self.path == "/api/users" and account[1] == "admin":
            return self.json_reply(200, ACCOUNTS.list_users())
        if re.fullmatch(r"/api/evidence/[0-9a-f]{32}", self.path) and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT content, sha256 FROM evidence_files WHERE id=?", (self.path.rsplit("/", 1)[1],)).fetchone()
            if not row:
                return self.reply(404, b"{}")
            if hashlib.sha256(row[0]).hexdigest() != row[1]:
                return self.reply(500, b'{"error":"integrity"}')
            return self.reply(200, row[0], "application/octet-stream")
        if self.path == "/api/annual-totals" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            totals = annual_totals(json.loads(row[0])) if row else []
            return self.reply(200, json.dumps(totals, ensure_ascii=False).encode("utf-8"))
        report = re.fullmatch(r"/api/report/([0-9]{4})", self.path)
        if report and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            if row is None:
                return self.reply(404, b"{}")
            html = annual_report(json.loads(row[0]), report.group(1))
            return self.reply(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if self.path == "/api/quality" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            if row is None:
                return self.reply(200, b"[]")
            try:
                issues = inventory_quality.check(json.loads(row[0]))
                return self.reply(200, json.dumps(issues, ensure_ascii=False).encode("utf-8"))
            except (ValueError, TypeError, OverflowError):
                return self.reply(400, b'{"error":"invalid inventory"}')
        if self.path == "/api/history" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                rows = con.execute("SELECT id,changed_at,operation,collection,item_id,before_json,after_json,reason,actor FROM change_history ORDER BY id DESC LIMIT 500").fetchall()
            keys = ("id", "changed_at", "operation", "collection", "item_id", "before", "after", "reason", "actor")
            return self.reply(200, json.dumps([dict(zip(keys, row)) for row in rows], ensure_ascii=False).encode("utf-8"))
        if self.path == "/api/backups" and account[1] == "admin":
            return self.reply(200, json.dumps(self.backup_names()).encode("utf-8"))
        if self.path == "/api/inventory" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            self.response_revision = revision(row[0] if row else None)
            return self.reply(200, (row[0] if row else "null").encode())
        return self.reply(404, b"{}")

    def do_POST(self):
        if not self.allowed() or self.headers.get("X-GHG-Token") != TOKEN:
            return self.reply(403, b"{}")
        if self.path in ("/api/setup", "/api/login", "/api/logout", "/api/users", "/api/users/update"):
            return self.auth_action()
        account = self.identified()
        if not account:
            return self.reply(403, b"{}")
        self.actor, role = account
        if self.path.startswith(("/api/migrate-v1/", "/api/backups/")) and role != "admin":
            return self.reply(403, b"{}")
        if self.path in ("/api/evidence/upload", "/api/xlsx/read") and role not in ("admin", "editor"):
            return self.reply(403, b"{}")

        if self.path in ("/api/migrate-v1/preview", "/api/migrate-v1/apply"):
            if not self.allowed() or self.headers.get("X-GHG-Token") != TOKEN:
                return self.reply(403, b"{}")
            return self.migrate_v1()
        if self.allowed() and self.headers.get("X-GHG-Token") == TOKEN and self.path == "/api/evidence/upload":
            return self.evidence_upload()
        if self.allowed() and self.headers.get("X-GHG-Token") == TOKEN and self.path in ("/api/backups/create", "/api/backups/restore"):
            return self.backup_action()
        if not self.allowed() or self.headers.get("X-GHG-Token") != TOKEN or self.path not in ("/api/xlsx/read", "/api/xlsx/write"):
            return self.reply(403, b"{}")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= xlsx_local.MAX_FILE:
                raise ValueError("檔案太大或沒有內容")
            data = self.rfile.read(size)
            if self.path == "/api/xlsx/read":
                matrix = xlsx_local.read_xlsx(data)
                return self.reply(200, json.dumps(matrix, ensure_ascii=False).encode("utf-8"))
            matrix = json.loads(data)
            body = xlsx_local.write_xlsx(matrix)
            return self.reply(200, body, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except (ValueError, TypeError, OSError, OverflowError) as e:
            return self.reply(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))

    def migrate_v1(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 20_000_000:
                raise ValueError("請選擇 20 MB 以下的 V1 JSON 備份")
            data = json.loads(self.rfile.read(size).decode("utf-8-sig"))
            validate(data)
            if set(data) != {"settings", *KEYS}:
                raise ValueError("請使用 V1.0 的 JSON 備份")
            if not re.fullmatch(r"[0-9]{4}", str(data["settings"].get("year", ""))):
                raise ValueError("盤查年度格式不正確")
            for key in KEYS:
                ids = [item.get("id") for item in data[key]]
                if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in ids):
                    raise ValueError("舊版紀錄編號格式不正確")
                if len(set(ids)) != len(ids):
                    raise ValueError("備份中有重複的紀錄編號")
            if any(type(item.get("cat")) is not int or not 1 <= item["cat"] <= 6 for item in data["sources"]):
                raise ValueError("備份中有無效的排放類別")
            if any(not isinstance(item.get("month"), str) for item in data["records"]):
                raise ValueError("備份中有無效的活動月份")
            payload = validate(data)
            fingerprint = revision(payload)
            with connect() as con:
                if con.execute("SELECT 1 FROM inventory WHERE id=1").fetchone():
                    return self.reply(409, '{"error":"移轉只能匯入尚未儲存資料的 V2 資料庫"}'.encode())
                if self.path.endswith("/apply"):
                    if self.headers.get("X-GHG-Import-Preview") != fingerprint:
                        return self.reply(409, '{"error":"檔案與預覽不同，請重新預覽"}'.encode())
                    con.execute("INSERT INTO inventory VALUES(1, ?)", (payload,))
                    write_history(con, None, payload, "V1 資料移轉", self.actor)
            return self.reply(200, json.dumps({
                "year": data["settings"]["year"], "counts": {key: len(data[key]) for key in KEYS},
                "preview": fingerprint,
            }).encode())
        except (UnicodeError, ValueError, TypeError, RecursionError):
            return self.reply(400, '{"error":"V1 備份格式或紀錄欄位不正確"}'.encode())
        except (sqlite3.Error, OSError):
            return self.reply(500, '{"error":"無法讀寫本機資料庫，移轉未完成"}'.encode())

    def evidence_upload(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 7_100_000:
                raise ValueError("檔案過大或沒有內容")
            obj = json.loads(self.rfile.read(size))
            if not isinstance(obj, dict):
                raise ValueError("資料格式不正確")
            filename, name, note, record_id, encoded = (obj.get(k) for k in ("filename", "name", "note", "recordId", "content"))
            if any(not isinstance(x, str) for x in (filename, name, note, record_id, encoded)):
                raise ValueError("欄位格式不正確")
            if not filename.strip() or len(filename) > 255 or any(ord(c) < 32 for c in filename) or '/' in filename or chr(92) in filename:
                raise ValueError("檔名不正確")
            if not name.strip() or len(name) > 255 or len(note) > 2000 or len(record_id) > 255:
                raise ValueError("文件名稱或備註不正確")
            content = base64.b64decode(encoded, validate=True)
            if not 0 < len(content) <= 5_000_000:
                raise ValueError("檔案大小須介於 1 位元組至 5 MB")
            digest = hashlib.sha256(content).hexdigest()
            file_id = secrets.token_hex(16)
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
                previous = row[0] if row else None
                if self.headers.get("X-GHG-Revision") != revision(previous):
                    return self.reply(409, b'{"error":"stale revision"}')
                data = json.loads(previous) if previous else None
                if not data or not any(r.get("id") == record_id for r in data.get("records", [])):
                    raise ValueError("找不到對應活動紀錄")
                data["evidence"].append({"id": secrets.token_hex(16), "recordId": record_id,
                    "name": name.strip(), "location": "本機資料庫", "note": note,
                    "fileId": file_id, "filename": filename, "sha256": digest})
                payload = validate(data)
                backup_database(con)
                con.execute("INSERT INTO evidence_files(id,filename,sha256,content) VALUES(?,?,?,?)", (file_id, filename, digest, content))
                con.execute("UPDATE inventory SET payload=? WHERE id=1", (payload,))
                write_history(con, previous, payload, "佐證上傳", self.actor)
            self.response_revision = revision(payload)
            return self.reply(200, b"{}")
        except (ValueError, TypeError, sqlite3.Error, OSError, OverflowError, binascii.Error):
            return self.reply(400, b'{"error":"invalid evidence"}')

    def backup_action(self):
        try:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
                previous = row[0] if row else None
                if previous is None:
                    raise ValueError("目前沒有可備份的資料")
                if self.headers.get("X-GHG-Revision") != revision(previous):
                    return self.reply(409, b'{"error":"stale revision"}')
                if self.path == "/api/backups/create":
                    backup_database(con)
                    self.response_revision = revision(previous)
                    return self.reply(200, b"{}")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024:
                    raise ValueError("備份名稱不正確")
                obj = json.loads(self.rfile.read(size))
                name = obj.get("name") if isinstance(obj, dict) else None
                if not isinstance(name, str) or name not in self.backup_names():
                    raise ValueError("找不到選取的備份")
                path = BACKUPS / name
                # Read the selected file before creating another backup, which may rotate older files.
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("備份資料庫檢查失敗")
                    saved = source.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
                    if not saved or len(saved[0]) > 50_000_000:
                        raise ValueError("備份內容不正確")
                    restored = json.loads(saved[0])
                    payload = validate(restored)
                    evidence_files = []
                    for entry in restored["evidence"]:
                        file_id = entry.get("fileId")
                        if not file_id:
                            continue
                        if not isinstance(file_id, str) or not re.fullmatch(r"[0-9a-f]{32}", file_id):
                            raise ValueError("備份佐證編號不正確")
                        blob = source.execute("SELECT filename,sha256,content FROM evidence_files WHERE id=?", (file_id,)).fetchone()
                        if not blob or len(blob[2]) > 5_000_000 or hashlib.sha256(blob[2]).hexdigest() != blob[1] or entry.get("sha256") != blob[1]:
                            raise ValueError("備份佐證檔案遺失或損壞")
                        evidence_files.append((file_id, *blob))
                backup_database(con)
                for item in evidence_files:
                    con.execute("INSERT INTO evidence_files(id,filename,sha256,content) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,sha256=excluded.sha256,content=excluded.content", item)
                con.execute("UPDATE inventory SET payload=? WHERE id=1", (payload,))
                write_history(con, previous, payload, "備份還原", self.actor)
            self.response_revision = revision(payload)
            return self.reply(200, b"{}")
        except (ValueError, TypeError, sqlite3.Error, OSError, OverflowError) as e:
            return self.reply(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))

    def do_PUT(self):
        account = self.identified()
        if not account or account[1] == "viewer":
            return self.reply(403, b"{}")
        self.actor, role = account
        if not self.allowed() or self.path != "/api/inventory" or self.headers.get("X-GHG-Token") != TOKEN:
            return self.reply(403, b"{}")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 50_000_000:
                raise ValueError("Invalid size")
            payload = validate(json.loads(self.rfile.read(size)))
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
                previous = row[0] if row else None
                if self.headers.get("X-GHG-Revision") != revision(previous):
                    return self.reply(409, b'{"error":"stale revision"}')
                if not review_permissions(previous, payload, role):
                    return self.json_reply(403, {"error": "此帳號無權進行這項變更。"})
                protect_reviewed(previous, payload)
                if previous is not None and previous != payload:
                    backup_database(con)
                con.execute("INSERT INTO inventory VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))
                write_history(con, previous, payload, "資料儲存", self.actor)
            self.response_revision = revision(payload)
            self.reply(200, b"{}")
        except (ValueError, TypeError, sqlite3.Error, OSError):
            self.reply(400, b"{}")

if __name__ == "__main__":
    with connect():
        pass
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        url = "http://127.0.0.1:" + str(server.server_port)
        print("GHG local preview:", url, "\nDatabase:", DB, "\nKeep this window open.")
        webbrowser.open(url)
        server.serve_forever()
