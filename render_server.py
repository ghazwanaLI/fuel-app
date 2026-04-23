#!/usr/bin/env python3
"""
نظام تدقيق اوليات المحطات الاهلية
نسخة Render.com مع PostgreSQL
"""
import json, os, hashlib, uuid, base64, io, threading, queue
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import pg8000

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """خادم متعدد الخيوط — يعالج كل طلب في خيط مستقل"""
    daemon_threads = True

# ── SSE ──
_sse_clients = []
_sse_lock = threading.Lock()
_version = 0

def sse_broadcast(event="update"):
    global _version
    _version += 1
    msg = f"{event}:{_version}"
    dead = []
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try: q.put(msg)
        except: dead.append(q)
    if dead:
        with _sse_lock:
            for q in dead:
                if q in _sse_clients: _sse_clients.remove(q)

PORT = int(os.environ.get("PORT", 8080))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def get_conn():
    import urllib.parse
    r = urllib.parse.urlparse(DATABASE_URL)
    return pg8000.connect(
        host=r.hostname, port=r.port or 5432,
        database=r.path.lstrip("/"),
        user=r.username, password=r.password,
        ssl_context=True
    )

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS db_store (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS db_files (
            key TEXT PRIMARY KEY,
            name TEXT,
            data TEXT,
            mime TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS db_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS db_logs (
            id SERIAL PRIMARY KEY,
            user_name TEXT,
            user_fullname TEXT,
            action TEXT,
            details TEXT,
            ip TEXT,
            created_at TIMESTAMP DEFAULT NOW()
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
                    "fullname": "مدير النظام",
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

def save_file(key, name, data, mime):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO db_files (key, name, data, mime)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET name=%s, data=%s, mime=%s
        """, [key, name, data, mime, name, data, mime])
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cur.close()
        conn.close()

def load_file(key):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, data, mime FROM db_files WHERE key=%s", [key])
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"name": row[0], "data": row[1], "mime": row[2]}
    return None

def save_session(token, user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO db_sessions (token, user_id) VALUES (%s, %s) ON CONFLICT (token) DO UPDATE SET user_id=%s", [token, user_id, user_id])
    conn.commit(); cur.close(); conn.close()

def get_session(token):
    if not token: return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM db_sessions WHERE token=%s", [token])
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None

def delete_session(token):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM db_sessions WHERE token=%s", [token])
    conn.commit(); cur.close(); conn.close()



sessions = {}

def add_log(user, action, details, ip=""):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO db_logs (user_name, user_fullname, action, details, ip) VALUES (%s, %s, %s, %s, %s)",
            [user.get("username",""), user.get("fullname",""), action, details, ip]
        )
        conn.commit()
        cur.close()
        conn.close()
    except: pass

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
        if length == 0: return {}
        data = b""
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk: break
            data += chunk
            remaining -= len(chunk)
        return json.loads(data.decode("utf-8"))

    def get_token(self):
        # EventSource لا يدعم headers — نقرأ التوكن من query param أيضاً
        token = self.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            qs = parse_qs(urlparse(self.path).query)
            token = qs.get("t", [""])[0]
        return token

    def get_user(self):
        uid = get_session(self.get_token())
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
        
        # PWA files
        static_files = {
            '/manifest.json': ('manifest.json', 'application/json'),
            '/sw.js': ('sw.js', 'application/javascript'),
            '/icon-192.png': ('icon-192.png', 'image/png'),
            '/icon-512.png': ('icon-512.png', 'image/png'),
        }
        if p in static_files:
            fname, mime = static_files[p]
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
            if os.path.exists(fpath):
                with open(fpath, 'rb') as f:
                    body = f.read()
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({'error': 'not found'}, 404)
            return

        if p in ("", "/"):
            html_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(html_file, "r", encoding="utf-8") as f:
                self.send_html(f.read())
            return

        if p == "/api/events":
            u = self.require_auth()
            if not u: return
            q = queue.Queue()
            with _sse_lock:
                _sse_clients.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(f"data: connected:{_version}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    except:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except: pass
            finally:
                with _sse_lock:
                    if q in _sse_clients: _sse_clients.remove(q)
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
            f = load_file(f"{sid}_{ftype}")
            self.send_json({"ok": True, "file": f})
        elif p == "/api/logs":
            u = self.require_auth()
            if not u: return
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            limit = int(urlparse(self.path).query.replace("limit=","") or 100)
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT id,user_name,user_fullname,action,details,ip,created_at FROM db_logs ORDER BY created_at DESC LIMIT %s", [limit])
            rows = cur.fetchall()
            cur.close(); conn.close()
            logs = [{"id":r[0],"username":r[1],"fullname":r[2],"action":r[3],"details":r[4],"ip":r[5],"time":str(r[6])} for r in rows]
            self.send_json({"ok":True,"logs":logs})
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
            save_session(token, user["id"])
            self.send_json({"ok": True, "token": token, "user": {k: v for k, v in user.items() if k != "password"}})
            return

        if p == "/api/logout":
            delete_session(self.get_token())
            self.send_json({"ok": True}); return

        u = self.require_auth()
        if not u: return

        if p == "/api/stations":
            if not self.can(u, "edit"): self.send_json({"error": "لا صلاحية إضافة"}, 403); return
            body = self.read_body()
            if not body.get("name"): self.send_json({"error": "اسم المحطة مطلوب"}, 400); return
            db = load_db()
            sid = db["next_station_id"]; db["next_station_id"] += 1
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            station = {
                "id": sid, "name": body["name"], "phone": body.get("phone", ""),
                "tax":       body.get("tax",       empty_doc()),
                "guarantee": body.get("guarantee", empty_doc()),
                "social":    body.get("social",    empty_doc()),
                "created_by": u["fullname"],
                "updated_by": u["fullname"],
                "updated_at": now,
            }
            db["stations"].append(station); save_db(db); sse_broadcast()
            ip = self.headers.get("X-Forwarded-For", self.client_address[0])
            add_log(u, "إضافة محطة", f"أضاف محطة: {station['name']}", ip)
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
            db["users"].append(new_user); save_db(db); sse_broadcast()
            ip = self.headers.get("X-Forwarded-For", self.client_address[0])
            add_log(u, "إضافة مستخدم", f"أضاف مستخدم: {new_user['fullname']} ({new_user['username']})", ip)
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
                save_db(db); sse_broadcast()
                self.send_json({"ok": True, "added": added, "skipped": skipped})
            except Exception as e:
                self.send_json({"error": f"خطأ في قراءة الملف: {str(e)}"}, 400)

        elif p.startswith("/api/files/"):
            if not self.can(u, "files"): self.send_json({"error": "لا صلاحية رفع"}, 403); return
            parts = p.split("/")
            if len(parts) < 5: self.send_json({"error": "مسار خاطئ"}, 400); return
            sid, ftype = parts[3], parts[4]
            try:
                body = self.read_body()
                save_file(f"{sid}_{ftype}", body.get("name",""), body.get("data",""), body.get("mime",""))
                ip = self.headers.get("X-Forwarded-For", self.client_address[0])
                add_log(u, "رفع ملف", f"رفع ملف: {body.get('name','')} للمحطة {sid}", ip)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": f"خطأ في حفظ الملف: {str(e)}"}, 500)
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
            from datetime import datetime
            s["updated_by"] = u["fullname"]
            s["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            ip = self.headers.get("X-Forwarded-For", self.client_address[0])
            add_log(u, "تعديل محطة", f"عدّل محطة: {s['name']}", ip)
            save_db(db); sse_broadcast(); self.send_json({"ok": True, "station": s})

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
                ip = self.headers.get("X-Forwarded-For", self.client_address[0])
                add_log(u, "تغيير كلمة المرور", f"غيّر كلمة مرور المستخدم id={uid}", ip)
            for f in ["fullname", "username", "role", "active", "perms"]:
                if f in body: db["users"][idx][f] = body[f]
            save_db(db)
            sse_broadcast()
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
            save_db(db); sse_broadcast(); self.send_json({"ok": True})

        elif p.startswith("/api/users/"):
            if u["role"] != "admin": self.send_json({"error": "غير مصرح"}, 403); return
            uid = int(p.split("/")[-1])
            if uid == u["id"]: self.send_json({"error": "لا يمكن حذف حسابك"}, 400); return
            db = load_db()
            deleted_u = next((x for x in db["users"] if x["id"]==uid), {})
            db["users"] = [x for x in db["users"] if x["id"] != uid]
            save_db(db)
            ip = self.headers.get("X-Forwarded-For", self.client_address[0])
            add_log(u, "حذف مستخدم", f"حذف مستخدم: {deleted_u.get('fullname','')}", ip)
            self.send_json({"ok": True})

        elif p.startswith("/api/files/"):
            if not self.can(u, "files"): self.send_json({"error": "لا صلاحية"}, 403); return
            parts = p.split("/"); sid, ftype = parts[3], parts[4]
            delete_file(f"{sid}_{ftype}")
            self.send_json({"ok": True})
        else:
            self.send_json({"error": "غير موجود"}, 404)

if __name__ == "__main__":
    print("⏳ جاري تهيئة قاعدة البيانات...")
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"✅ السيرفر يعمل على المنفذ {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
