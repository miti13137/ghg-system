"""GHG V2 local storage prototype. Python 3.10+, standard library only."""
import json
import hashlib
import time
import os
import sqlite3
import secrets
import webbrowser
import xlsx_local
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
        if self.path == "/api/inventory" and self.headers.get("X-GHG-Token") == TOKEN:
            with connect() as con:
                row = con.execute("SELECT payload FROM inventory WHERE id=1").fetchone()
            self.response_revision = revision(row[0] if row else None)
            return self.reply(200, (row[0] if row else "null").encode())
        return self.reply(404, b"{}")

    def do_POST(self):
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
                if previous is not None and previous != payload:
                    backup_database(con)
                con.execute("INSERT INTO inventory VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))
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
