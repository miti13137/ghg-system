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
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "GHGSystem"
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "inventory.sqlite3"
TOKEN = secrets.token_urlsafe(32)
BACKUPS = DATA / "backups"
KEYS = ("sources", "factors", "records", "evidence", "audit")

def connect():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS evidence_files (id TEXT PRIMARY KEY, filename TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS change_history (id INTEGER PRIMARY KEY AUTOINCREMENT, changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), operation TEXT NOT NULL, collection TEXT NOT NULL, item_id TEXT NOT NULL, before_json TEXT, after_json TEXT, reason TEXT NOT NULL DEFAULT '')")
    return con

def validate(data):
    if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
        raise ValueError("Invalid inventory settings")
    for key in KEYS:
        if not isinstance(data.get(key), list) or any(not isinstance(x, dict) for x in data[key]):
            raise ValueError("Invalid inventory collection: " + key)
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
    for collection in KEYS:
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


def write_history(con, previous, payload, reason):
    events = change_events(previous, payload, reason)
    if not events and reason == "備份還原":
        events = [("還原", "inventory", "(全部)", None, None, reason)]
    con.executemany("INSERT INTO change_history(operation,collection,item_id,before_json,after_json,reason) VALUES(?,?,?,?,?,?)", events)


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

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, b"{}")
        if self.path == "/":
            html = (ROOT / "index.html").read_text(encoding="utf-8")
            bridge = """<script>
const localToken = "__TOKEN__";
let localRevision = null;
function localRequest(method, data) {
  const req = new XMLHttpRequest();
  req.open(method, "/api/inventory", false);
  req.setRequestHeader("X-GHG-Token", localToken);
  if (method === "PUT") { req.setRequestHeader("Content-Type", "application/json"); req.setRequestHeader("X-GHG-Revision", localRevision || ""); }
  req.send(data || null);
  if (req.status === 409) { alert("資料已由其他視窗變更，本次未儲存。請重新整理後再操作。"); throw Error("資料版本衝突，本次未儲存"); }
  if (req.status !== 200) { alert("資料儲存或讀取失敗，本次變更可能未儲存。請保留畫面並聯絡管理人員。"); throw Error("Local database request failed: " + req.status); }
  localRevision = req.getResponseHeader("X-GHG-Revision");
  return req.responseText;
}
const localStore = {
  getItem: () => { const raw = localRequest("GET"); return raw === "null" ? null : raw; },
  setItem: (key, value) => { localRequest("PUT", value); }
};
</script>""".replace("__TOKEN__", TOKEN)
            html = html.replace('<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>', "")
            html = html.replace("Excel 功能載入失敗，請確認網路連線後重新整理。", "離線預覽版尚未提供 Excel 功能，請使用 CSV 或 JSON 備份。")
            html = html.replace("<script>", bridge + "<script>", 1).replace("localStorage.getItem", "localStore.getItem").replace("localStorage.setItem", "localStore.setItem")
            html = html.replace("企業溫室氣體盤查管理系統 V1.0", "企業溫室氣體盤查管理系統 V2.0 Local Preview")
            html = html.replace("本版資料儲存在此瀏覽器的 localStorage，尚未提供登入、多人同步、真正檔案儲存或雲端備份。", "本機預覽版使用 SQLite 儲存；尚未提供登入或多人同步。")
            html = html.replace("目前仍使用本機瀏覽器儲存", "目前使用本機 SQLite 儲存")
            html = html.replace("catch{db=fresh()}", "catch(e){document.body.textContent='資料庫讀取失敗，請勿繼續操作。';throw e}")
            return self.reply(200, html.encode(), "text/html; charset=utf-8")
        if re.fullmatch(r"/api/evidence/[0-9a-f]{32}", self.path) and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT content, sha256 FROM evidence_files WHERE id=?", (self.path.rsplit("/", 1)[1],)).fetchone()
            if not row:
                return self.reply(404, b"{}")
            if hashlib.sha256(row[0]).hexdigest() != row[1]:
                return self.reply(500, b'{"error":"integrity"}')
            return self.reply(200, row[0], "application/octet-stream")
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
                rows = con.execute("SELECT id,changed_at,operation,collection,item_id,before_json,after_json,reason FROM change_history ORDER BY id DESC LIMIT 500").fetchall()
            keys = ("id", "changed_at", "operation", "collection", "item_id", "before", "after", "reason")
            return self.reply(200, json.dumps([dict(zip(keys, row)) for row in rows], ensure_ascii=False).encode("utf-8"))
        if self.path == "/api/backups" and self.headers.get("X-GHG-Token") == TOKEN:
            return self.reply(200, json.dumps(self.backup_names()).encode("utf-8"))
        if self.path == "/api/inventory" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            self.response_revision = revision(row[0] if row else None)
            return self.reply(200, (row[0] if row else "null").encode())
        return self.reply(404, b"{}")

    def do_POST(self):
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
                write_history(con, previous, payload, "佐證上傳")
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
                write_history(con, previous, payload, "備份還原")
            self.response_revision = revision(payload)
            return self.reply(200, b"{}")
        except (ValueError, TypeError, sqlite3.Error, OSError, OverflowError) as e:
            return self.reply(400, json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))

    def do_PUT(self):
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
                protect_reviewed(previous, payload)
                if previous is not None and previous != payload:
                    backup_database(con)
                con.execute("INSERT INTO inventory VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))
                write_history(con, previous, payload, "資料儲存")
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
