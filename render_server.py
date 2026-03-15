#!/usr/bin/env python3
"""
نظام تدقيق اوليات المحطات الاهلية
نسخة Render.com مع PostgreSQL
"""
import json, os, hashlib, uuid, base64, io
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
import psycopg2
from psycopg2.extras import RealDictCursor

PORT = int(os.environ.get("PORT", 8080))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS db_store (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    # تهيئة البيانات الافتراضية
    cur.execute("SELECT value FROM db_store WHERE key='data'")
    row = cur.fetchone()
    if not row:
        default = {
            "stations": [],
            "users": [
                {
                    "id": 1,
                    "fullname": "المدير العام",
                    "username": "admin",
                    "password": hash_pw("admin123"),
                    "role": "admin",
                    "active": True,
                    "perms": {"view":True,"edit":True,"del":True,"files":True,"export":True,"docs":True}
                }
            ],
            "files": {},
            "next_station_id": 1,
            "next_user_id": 2
        }
        cur.execute("INSERT INTO db_store VALUES ('data', %s)", [json.dumps(default, ensure_ascii=False)])
        conn.commit()
    cur.close()
    conn.close()

def load_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT value FROM db_store WHERE key='data'")
    row = cur.fetchone()
    cur.close()
    conn.close()
    db = json.loads(row[0])
    for s in db.get("stations", []):
        if "phone" not in s:
            s["phone"] = ""
    return db

def save_db(db):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE db_store SET value=%s WHERE key='data'", [json.dumps(db, ensure_ascii=False)])
    conn.commit()
    cur.close()
    conn.close()

sessions = {}

def empty_doc():
    return {"bookNo": "", "bookDate": "", "expiry": "", "active": False}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def get_token(self):
        return self.headers.get("Authorization", "").replace("Bearer ", "").strip()

    def get_user(self):
        uid = sessions.get(self.get_token())
        if not uid: return None
        return next((u for u in load_db()["users"] if u["id"] == uid), None)

    def require_auth(self):
        u = self.get_user()
        if not u: self.send_json({"error": "غير مصرح"}, 401)
        return u

    def can(self, user, perm):
        if user["role"] == "admin": return True
        return bool(user.get("perms", {}).get(perm))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization")
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path.rstrip("/")
        if p in ("", "/"):
            html_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(html_file, "r", encoding="utf-8") as f:
                self.send_html(f.read())
            return

        if p == "/api/stations":
            u = self.require_auth()
            if not u: return
            self.send_json({"ok": True, "stations": load_db()["stations"]})

        elif p == "/api/me":
            u = self.require_auth()
            if not u: return
            self.send_json({"ok": True, "user": {k: v for k, v in u.items() if k != "password"}})

        elif p == "/api/users":
            u = self.require_auth()
            if not u: return
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            safe = [{k: v for k, v in x.items() if k != "password"} for x in load_db()["users"]]
            self.send_json({"ok": True, "users": safe})

        elif p.startswith("/api/files/"):
            u = self.require_auth()
            if not u: return
            if not self.can(u, "docs"): self.send_json({"error": "لا صلاحية"}, 403); return
            parts = p.split("/")
            if len(parts) < 5: self.send_json({"error": "مسار خاطئ"}, 400); return
            sid, ftype = parts[3], parts[4]
            f = load_db()["files"].get(f"{sid}_{ftype}")
            self.send_json({"ok": True, "file": f})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")

        if p == "/api/login":
            body = self.read_body()
            db = load_db()
            user = next((u for u in db["users"]
                         if u["username"] == body.get("username")
                         and u["password"] == hash_pw(body.get("password", ""))
                         and u.get("active", True)), None)
            if not user: self.send_json({"error": "اسم المستخدم أو كلمة المرور غير صحيحة"}, 401); return
            token = str(uuid.uuid4())
            sessions[token] = user["id"]
            self.send_json({"ok": True, "token": token, "user": {k: v for k, v in user.items() if k != "password"}})
            return

        if p == "/api/logout":
            sessions.pop(self.get_token(), None)
            self.send_json({"ok": True}); return

        u = self.require_auth()
        if not u: return

        if p == "/api/stations":
            if not self.can(u, "edit"): self.send_json({"error": "لا صلاحية إضافة"}, 403); return
            body = self.read_body()
            if not body.get("name"): self.send_json({"error": "اسم المحطة مطلوب"}, 400); return
            db = load_db()
            sid = db["next_station_id"]; db["next_station_id"] += 1
            station = {
                "id": sid, "name": body["name"], "phone": body.get("phone", ""),
                "tax":       body.get("tax",       empty_doc()),
                "guarantee": body.get("guarantee", empty_doc()),
                "social":    body.get("social",    empty_doc()),
            }
            db["stations"].append(station); save_db(db)
            self.send_json({"ok": True, "station": station})

        elif p == "/api/users":
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            body = self.read_body(); db = load_db()
            if any(x["username"] == body.get("username") for x in db["users"]):
                self.send_json({"error": "اسم المستخدم مستخدم بالفعل"}, 400); return
            uid = db["next_user_id"]; db["next_user_id"] += 1
            role = body.get("role", "viewer")
            new_user = {
                "id": uid, "fullname": body.get("fullname", ""),
                "username": body.get("username", ""),
                "password": hash_pw(body.get("password", "")),
                "role": role, "active": True,
                "perms": {"view":True,"edit":True,"del":True,"files":True,"export":True,"docs":True}
                         if role == "admin" else body.get("perms", {"view":True,"edit":False,"del":False,"files":False,"export":False,"docs":True})
            }
            db["users"].append(new_user); save_db(db)
            self.send_json({"ok": True, "user": {k: v for k, v in new_user.items() if k != "password"}})

        elif p == "/api/import-excel":
            if not self.can(u, "edit"): self.send_json({"error": "لا صلاحية استيراد"}, 403); return
            body = self.read_body()
            excel_b64 = body.get("data", "")
            if not excel_b64: self.send_json({"error": "لا يوجد ملف"}, 400); return
            try:
                import openpyxl
                excel_bytes = base64.b64decode(excel_b64)
                wb = openpyxl.load_workbook(io.BytesIO(excel_bytes))
                ws = wb.active
                db = load_db()
                added = 0; skipped = 0
                existing_names = {s["name"].strip() for s in db["stations"]}
                for row in ws.iter_rows(min_row=2, values_only=True):
                    name = None
                    for cell in row:
                        if cell and isinstance(cell, str) and cell.strip():
                            name = cell.strip(); break
                    if not name: skipped += 1; continue
                    if name in existing_names: skipped += 1; continue
                    sid = db["next_station_id"]; db["next_station_id"] += 1
                    db["stations"].append({
                        "id": sid, "name": name, "phone": "",
                        "tax":       empty_doc(), "guarantee": empty_doc(), "social": empty_doc(),
                    })
                    existing_names.add(name); added += 1
                save_db(db)
                self.send_json({"ok": True, "added": added, "skipped": skipped})
            except Exception as e:
                self.send_json({"error": f"خطأ في قراءة الملف: {str(e)}"}, 400)

        elif p.startswith("/api/files/"):
            if not self.can(u, "files"): self.send_json({"error": "لا صلاحية رفع"}, 403); return
            parts = p.split("/")
            if len(parts) < 5: self.send_json({"error": "مسار خاطئ"}, 400); return
            sid, ftype = parts[3], parts[4]
            body = self.read_body(); db = load_db()
            db["files"][f"{sid}_{ftype}"] = {"name": body.get("name", ""), "data": body.get("data", ""), "mime": body.get("mime", "")}
            save_db(db); self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_PUT(self):
        p = urlparse(self.path).path.rstrip("/")
        u = self.require_auth()
        if not u: return

        if p.startswith("/api/stations/"):
            if not self.can(u, "edit"): self.send_json({"error": "لا صلاحية تعديل"}, 403); return
            sid = int(p.split("/")[-1]); body = self.read_body(); db = load_db()
            idx = next((i for i, s in enumerate(db["stations"]) if s["id"] == sid), None)
            if idx is None: self.send_json({"error": "غير موجود"}, 404); return
            s = db["stations"][idx]
            if "name" in body: s["name"] = body["name"]
            if "phone" in body: s["phone"] = body["phone"]
            for doc in ["tax", "guarantee", "social"]:
                if doc in body: s[doc] = body[doc]
            save_db(db); self.send_json({"ok": True, "station": s})

        elif p.startswith("/api/users/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            uid = int(p.split("/")[-1]); body = self.read_body(); db = load_db()
            idx = next((i for i, x in enumerate(db["users"]) if x["id"] == uid), None)
            if idx is None: self.send_json({"error": "غير موجود"}, 404); return
            if "password" in body and body["password"]:
                if "old_password" in body:
                    if db["users"][idx]["password"] != hash_pw(body["old_password"]):
                        self.send_json({"error": "كلمة المرور الحالية غير صحيحة"}, 400); return
                db["users"][idx]["password"] = hash_pw(body["password"])
            for f in ["fullname", "username", "role", "active", "perms"]:
                if f in body: db["users"][idx][f] = body[f]
            save_db(db)
            self.send_json({"ok": True, "user": {k: v for k, v in db["users"][idx].items() if k != "password"}})
        else:
            self.send_json({"error": "غير موجود"}, 404)

    def do_DELETE(self):
        p = urlparse(self.path).path.rstrip("/")
        u = self.require_auth()
        if not u: return

        if p.startswith("/api/stations/"):
            if not self.can(u, "del"): self.send_json({"error": "لا صلاحية حذف"}, 403); return
            sid = int(p.split("/")[-1]); db = load_db()
            db["stations"] = [s for s in db["stations"] if s["id"] != sid]
            for t in ["tax", "guarantee", "social"]: db["files"].pop(f"{sid}_{t}", None)
            save_db(db); self.send_json({"ok": True})

        elif p.startswith("/api/users/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            uid = int(p.split("/")[-1])
            if uid == u["id"]: self.send_json({"error": "لا يمكن حذف حسابك"}, 400); return
            db = load_db()
            db["users"] = [x for x in db["users"] if x["id"] != uid]
            save_db(db); self.send_json({"ok": True})

        elif p.startswith("/api/files/"):
            if not self.can(u, "files"): self.send_json({"error": "لا صلاحية"}, 403); return
            parts = p.split("/"); sid, ftype = parts[3], parts[4]
            db = load_db(); db["files"].pop(f"{sid}_{ftype}", None)
            save_db(db); self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)

if __name__ == "__main__":
    print("⏳ جاري تهيئة قاعدة البيانات...")
    init_db()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✅ السيرفر يعمل على المنفذ {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
