from __future__ import annotations
import argparse, csv, getpass, hashlib, hmac, io, json, math, os, secrets, sqlite3, statistics, time
from contextlib import contextmanager
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "data"
DB_PATH = DB_DIR / "sarathi.sqlite3"
COOKIE = "sarathi_session"
PBKDF2_ROUNDS = 310_000
SESSION_SECONDS = 60 * 60 * 12
MAX_BODY = 1_100_000


@contextmanager
def connect():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('student','mentor')),
            password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS marks(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            subject TEXT NOT NULL, assessment TEXT NOT NULL, score REAL NOT NULL,
            maximum REAL NOT NULL, confidence INTEGER NOT NULL,
            assessment_date TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS marks_user_date ON marks(user_id, assessment_date);
        CREATE TABLE IF NOT EXISTS plans(
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            plan_json TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY, conversation_key TEXT NOT NULL,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_conversation_id ON messages(conversation_key, id);
        CREATE TABLE IF NOT EXISTS evaluations(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            dataset_name TEXT NOT NULL, rows_total INTEGER NOT NULL, mae REAL NOT NULL,
            rmse REAL NOT NULL, r2 REAL, baseline_mae REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS active_model(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            owner_id INTEGER NOT NULL REFERENCES users(id), model_json TEXT NOT NULL,
            rows_total INTEGER NOT NULL, mae REAL NOT NULL, rmse REAL NOT NULL,
            r2 REAL, baseline_mae REAL NOT NULL, trained_at TEXT NOT NULL,
            is_synthetic INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS rate_limits(
            key TEXT PRIMARY KEY, window_start INTEGER NOT NULL, attempts INTEGER NOT NULL
        );
        """)
        db.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
        model_columns = {r["name"] for r in db.execute("PRAGMA table_info(active_model)").fetchall()}
        if "is_synthetic" not in model_columns:
            db.execute("ALTER TABLE active_model ADD COLUMN is_synthetic INTEGER NOT NULL DEFAULT 0")
        demo = db.execute("SELECT id FROM users WHERE email=?", ("demo-model@sarathi.invalid",)).fetchone()
        if not demo:
            salt, digest = password_record(secrets.token_urlsafe(40))
            cur = db.execute("INSERT INTO users(email,display_name,role,password_hash,password_salt,created_at) VALUES(?,?,?,?,?,?)", ("demo-model@sarathi.invalid","Synthetic demo model","mentor",digest,salt,now_iso()))
            demo_id = cur.lastrowid
        else:
            demo_id = demo["id"]
        active = db.execute("SELECT singleton FROM active_model WHERE singleton=1").fetchone()
        sample_path = ROOT / "sample_evaluation.csv"
        if not active and sample_path.exists():
            demo_metrics = evaluate_csv(sample_path.read_text(encoding="utf-8"))
            db.execute("INSERT INTO active_model(singleton,owner_id,model_json,rows_total,mae,rmse,r2,baseline_mae,trained_at,is_synthetic) VALUES(1,?,?,?,?,?,?,?,?,1)", (demo_id,json.dumps(demo_metrics["model"],separators=(",",":")),demo_metrics["rows_total"],demo_metrics["mae"],demo_metrics["rmse"],demo_metrics["r2"],demo_metrics["baseline_mae"],now_iso()))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def password_record(password: str):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def password_matches(password: str, salt_hex: str, digest_hex: str):
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()
    return hmac.compare_digest(candidate, digest_hex)


def conversation_key(a: int, b: int):
    lo, hi = sorted((int(a), int(b)))
    return f"dm:{lo}:{hi}"


def to_public_user(row):
    return {"id": row["id"], "name": row["display_name"], "role": row["role"]}


def matrix_solve(A, b):
    n = len(b)
    M = [list(map(float, A[i])) + [float(b[i])] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-10:
            M[col][col] += 1e-5
            pivot = col
        M[col], M[pivot] = M[pivot], M[col]
        divisor = M[col][col]
        for j in range(col, n + 1):
            M[col][j] /= divisor
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col]
            for j in range(col, n + 1):
                M[r][j] -= factor * M[col][j]
    return [M[i][n] for i in range(n)]


def evaluate_csv(csv_text: str):
    if len(csv_text.encode("utf-8")) > MAX_BODY:
        raise ValueError("Dataset is too large. Keep the CSV under 1 MB.")
    reader = csv.DictReader(io.StringIO(csv_text))
    features = ["attendance", "assignments", "study_hours", "consistency", "confidence"]
    target = "final_score"
    required = features + [target]
    if not reader.fieldnames or any(col not in reader.fieldnames for col in required):
        raise ValueError("CSV needs columns: " + ", ".join(required))
    rows = []
    for line_no, raw in enumerate(reader, 2):
        if not any((v or "").strip() for v in raw.values()):
            continue
        try:
            vals = [float(raw[col]) for col in required]
        except (TypeError, ValueError):
            raise ValueError(f"Row {line_no} contains a missing or non-numeric value.")
        x, y = vals[:-1], vals[-1]
        if not all(math.isfinite(v) for v in vals):
            raise ValueError(f"Row {line_no} contains a non-finite number.")
        if not 0 <= x[0] <= 100 or not 0 <= x[1] <= 100 or not 0 <= x[2] <= 24 or not 1 <= x[3] <= 5 or not 1 <= x[4] <= 5 or not 0 <= y <= 100:
            raise ValueError(f"Row {line_no} has a value outside the allowed range.")
        rows.append((x, y))
        if len(rows) > 5000:
            raise ValueError("Maximum dataset size is 5,000 rows.")
    if len(rows) < 20:
        raise ValueError("Upload at least 20 complete rows to produce a holdout evaluation.")
    # Deterministic shuffle makes runs repeatable. Eighty percent trains; twenty percent is held out.
    order = list(range(len(rows)))
    secrets_generator = __import__("random").Random(42)
    secrets_generator.shuffle(order)
    split = max(1, int(len(rows) * 0.8))
    train_ids, test_ids = order[:split], order[split:]
    X = [rows[i][0] for i in train_ids]
    y = [rows[i][1] for i in train_ids]
    means = [statistics.mean(row[j] for row in X) for j in range(len(features))]
    stds = [statistics.pstdev(row[j] for row in X) or 1.0 for j in range(len(features))]
    design = [[1.0] + [(row[j] - means[j]) / stds[j] for j in range(len(features))] for row in X]
    # Small ridge penalty stabilizes correlated educational inputs; intercept is unpenalized.
    dim = len(features) + 1
    xtx = [[sum(row[i] * row[j] for row in design) + (0.25 if i == j and i > 0 else 0.0) for j in range(dim)] for i in range(dim)]
    xty = [sum(design[r][i] * y[r] for r in range(len(y))) for i in range(dim)]
    coeff = matrix_solve(xtx, xty)
    def predict(x):
        scaled = [1.0] + [(x[j] - means[j]) / stds[j] for j in range(len(features))]
        return max(0.0, min(100.0, sum(a * b for a, b in zip(coeff, scaled))))
    actual = [rows[i][1] for i in test_ids]
    predicted = [predict(rows[i][0]) for i in test_ids]
    baseline_value = statistics.mean(y)
    errors = [a - p for a, p in zip(actual, predicted)]
    mae = statistics.mean(abs(e) for e in errors)
    rmse = math.sqrt(statistics.mean(e * e for e in errors))
    baseline_mae = statistics.mean(abs(a - baseline_value) for a in actual)
    mean_actual = statistics.mean(actual)
    sst = sum((a - mean_actual) ** 2 for a in actual)
    r2 = (1 - sum(e * e for e in errors) / sst) if sst > 1e-12 else None
    model = {"features": features, "means": means, "stds": stds, "coefficients": coeff}
    return {"rows_total": len(rows), "train_rows": len(train_ids), "test_rows": len(test_ids),
            "mae": round(mae, 3), "rmse": round(rmse, 3), "r2": round(r2, 4) if r2 is not None else None,
            "baseline_mae": round(baseline_mae, 3), "better_than_baseline": mae < baseline_mae,
            "method": "Ridge linear regression; deterministic 80/20 shuffled holdout; features standardized on training data.",
            "model": model}


class SarathiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, handler, secure_cookies=False):
        self.secure_cookies = secure_cookies
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "SARATHI/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Keep logs useful without ever logging request bodies, which can contain credentials.
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")

    def json_response(self, status, data, set_cookie=None, clear_cookie=False):
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if set_cookie:
            c = cookies.SimpleCookie()
            c[COOKIE] = set_cookie
            c[COOKIE]["path"] = "/"
            c[COOKIE]["httponly"] = True
            c[COOKIE]["samesite"] = "Lax"
            c[COOKIE]["max-age"] = str(SESSION_SECONDS)
            if self.server.secure_cookies:
                c[COOKIE]["secure"] = True
            self.send_header("Set-Cookie", c.output(header="").strip())
        elif clear_cookie:
            c = cookies.SimpleCookie()
            c[COOKIE] = ""
            c[COOKIE]["path"] = "/"
            c[COOKIE]["httponly"] = True
            c[COOKIE]["samesite"] = "Lax"
            c[COOKIE]["max-age"] = "0"
            if self.server.secure_cookies:
                c[COOKIE]["secure"] = True
            self.send_header("Set-Cookie", c.output(header="").strip())
        self.end_headers()
        self.wfile.write(payload)

    def text_response(self, status, text, content_type="text/plain; charset=utf-8"):
        payload = text.encode("utf-8")
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY:
                raise ValueError("Invalid request size.")
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            return body
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON under 1 MB.")

    def session_token(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return jar[COOKIE].value if COOKIE in jar else None
        except cookies.CookieError:
            return None

    def get_session(self):
        token = self.session_token()
        if not token or len(token) != 64:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with connect() as db:
            row = db.execute("SELECT s.*,u.id AS uid,u.email,u.display_name,u.role FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (token_hash, int(time.time()))).fetchone()
        return dict(row) if row else None

    def new_session(self, user_id=None):
        token = secrets.token_hex(32)
        csrf = secrets.token_urlsafe(32)
        now = int(time.time())
        with connect() as db:
            db.execute("INSERT INTO sessions(token_hash,user_id,csrf,expires_at,created_at) VALUES(?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, now + SESSION_SECONDS, now))
        return token, csrf

    def delete_session(self, token):
        if token:
            with connect() as db:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def require_auth(self):
        session = self.get_session()
        if not session or not session.get("uid"):
            self.json_response(401, {"error": "Sign in to continue."})
            return None
        return session

    def check_csrf(self, session):
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            host = self.headers.get("Host", "")
            if parsed.netloc.lower() != host.lower() or parsed.scheme not in ("http", "https"):
                self.json_response(403, {"error": "Request origin rejected."})
                return False
        supplied = self.headers.get("X-SARATHI-CSRF", "")
        if not hmac.compare_digest(supplied, session["csrf"]):
            self.json_response(403, {"error": "Refresh the page and try again."})
            return False
        return True

    def rate_limited(self, key, limit=8, seconds=900):
        now = int(time.time())
        with connect() as db:
            row = db.execute("SELECT window_start,attempts FROM rate_limits WHERE key=?", (key,)).fetchone()
            if not row or now - row["window_start"] >= seconds:
                db.execute("INSERT INTO rate_limits(key,window_start,attempts) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,attempts=1", (key, now))
                return False
            if row["attempts"] >= limit:
                return True
            db.execute("UPDATE rate_limits SET attempts=attempts+1 WHERE key=?", (key,))
        return False

    def client_key(self, action):
        # Hash addresses before storage; do not retain raw client IPs in the database.
        addr = self.client_address[0] if self.client_address else "unknown"
        return hashlib.sha256((action + ":" + addr).encode()).hexdigest()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.serve_asset("index.html", "text/html; charset=utf-8")
        if path == "/assets/app.css":
            return self.serve_asset("app.css", "text/css; charset=utf-8")
        if path == "/assets/app.js":
            return self.serve_asset("app.js", "text/javascript; charset=utf-8")
        if path == "/assets/sample_evaluation.csv":
            return self.serve_asset("sample_evaluation.csv", "text/csv; charset=utf-8", attachment="sarathi_synthetic_demo_data.csv")
        if path == "/api/bootstrap":
            session = self.get_session()
            if not session:
                token, csrf = self.new_session()
                return self.json_response(200, {"user": None, "csrf": csrf}, set_cookie=token)
            user = None
            if session.get("uid"):
                user = {"id": session["uid"], "email": session["email"], "name": session["display_name"], "role": session["role"]}
            return self.json_response(200, {"user": user, "csrf": session["csrf"]})
        if path == "/api/marks":
            s = self.require_auth()
            if not s: return
            with connect() as db:
                rows = db.execute("SELECT id,subject,assessment,score,maximum,confidence,assessment_date FROM marks WHERE user_id=? ORDER BY assessment_date,id", (s["uid"],)).fetchall()
            return self.json_response(200, {"marks": [dict(row) for row in rows]})
        if path == "/api/plan":
            s = self.require_auth()
            if not s: return
            with connect() as db:
                row = db.execute("SELECT plan_json,updated_at FROM plans WHERE user_id=?", (s["uid"],)).fetchone()
            return self.json_response(200, {"plan": json.loads(row["plan_json"]) if row else None, "updated_at": row["updated_at"] if row else None})
        if path == "/api/model/status":
            s = self.require_auth()
            if not s: return
            with connect() as db:
                row = db.execute("SELECT rows_total,mae,rmse,r2,baseline_mae,trained_at,is_synthetic FROM active_model WHERE singleton=1").fetchone()
            return self.json_response(200, {"available": bool(row), "metrics": dict(row) if row else None})
        if path == "/api/chat/contacts":
            s = self.require_auth()
            if not s: return
            wanted_role = "mentor" if s["role"] == "student" else "student"
            with connect() as db:
                rows = db.execute("SELECT id,display_name,role FROM users WHERE role=? AND id<>? AND email<>? ORDER BY display_name COLLATE NOCASE", (wanted_role, s["uid"],"demo-model@sarathi.invalid")).fetchall()
            return self.json_response(200, {"contacts": [to_public_user(row) for row in rows]})
        if path == "/api/chat/messages":
            s = self.require_auth()
            if not s: return
            qs = parse_qs(urlparse(self.path).query)
            try: other = int(qs.get("contact", [""])[0])
            except ValueError: return self.json_response(400, {"error": "Choose a chat contact."})
            if not self.valid_contact(s, other): return
            key = conversation_key(s["uid"], other)
            with connect() as db:
                rows = db.execute("SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.display_name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.conversation_key=? ORDER BY m.id DESC LIMIT 100", (key,)).fetchall()
            return self.json_response(200, {"messages": [dict(row) for row in reversed(rows)]})
        if path == "/api/chat/stream":
            return self.chat_stream()
        if path == "/api/evaluations":
            s = self.require_auth()
            if not s: return
            if s["role"] != "mentor": return self.json_response(403, {"error": "Model evaluation is available to mentor accounts."})
            with connect() as db:
                rows = db.execute("SELECT id,dataset_name,rows_total,mae,rmse,r2,baseline_mae,created_at FROM evaluations WHERE user_id=? ORDER BY id DESC LIMIT 20", (s["uid"],)).fetchall()
            return self.json_response(200, {"evaluations": [dict(row) for row in rows]})
        return self.json_response(404, {"error": "Not found."})

    def serve_asset(self, filename, content_type, attachment=None):
        path = ROOT / filename
        try: data = path.read_bytes()
        except OSError: return self.text_response(404, "Missing portal asset.")
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if attachment: self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers(); self.wfile.write(data)

    def valid_contact(self, session, other_id):
        if other_id == session["uid"]:
            self.json_response(400, {"error": "Choose another account."}); return False
        allowed_role = "mentor" if session["role"] == "student" else "student"
        with connect() as db:
            contact = db.execute("SELECT id FROM users WHERE id=? AND role=?", (other_id, allowed_role)).fetchone()
        if not contact:
            self.json_response(404, {"error": "That chat contact is not available."}); return False
        return True

    def chat_stream(self):
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.netloc.lower() != self.headers.get("Host", "").lower() or parsed.scheme not in ("http", "https"):
                return self.json_response(403, {"error": "Request origin rejected."})
        s = self.require_auth()
        if not s: return
        qs = parse_qs(urlparse(self.path).query)
        try: other = int(qs.get("contact", [""])[0]); after = max(0, int(qs.get("after", ["0"])[0]))
        except ValueError: return self.json_response(400, {"error": "Invalid chat stream request."})
        try: after = max(after, int(self.headers.get("Last-Event-ID", "0")))
        except ValueError: pass
        if not self.valid_contact(s, other): return
        key = conversation_key(s["uid"], other)
        self.send_response(200); self.headers_common()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.end_headers()
        deadline = time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                with connect() as db:
                    rows = db.execute("SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.display_name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.conversation_key=? AND m.id>? ORDER BY m.id LIMIT 50", (key, after)).fetchall()
                if rows:
                    for row in rows:
                        item = dict(row); after = item["id"]
                        self.wfile.write(("id: %d\ndata: %s\n\n" % (after, json.dumps(item, ensure_ascii=False))).encode("utf-8"))
                    self.wfile.flush()
                    deadline = time.monotonic() + 25
                else:
                    self.wfile.write(b": keep-alive\n\n"); self.wfile.flush()
                    time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ("/api/signup", "/api/login"):
            session = self.get_session()
            if not session:
                token, csrf = self.new_session()
                return self.json_response(403, {"error": "Refresh and try again."}, set_cookie=token)
            if not self.check_csrf(session): return
            action = "signup" if path.endswith("signup") else "login"
            if self.rate_limited(self.client_key(action), limit=6):
                return self.json_response(429, {"error": "Too many attempts. Wait 15 minutes and try again."})
            try: data = self.read_json()
            except ValueError as e: return self.json_response(400, {"error": str(e)})
            email = str(data.get("email", "")).strip().lower()
            password = data.get("password", "")
            if not isinstance(password, str): return self.json_response(400, {"error": "Invalid password."})
            if action == "signup":
                name = str(data.get("name", "")).strip()[:60]
                # Public registration is student-only; mentor access is locally provisioned.
                role = "student"
                if len(name) < 2 or "@" not in email or len(email) > 254:
                    return self.json_response(400, {"error": "Enter a valid email and a name of at least 2 characters."})
                if len(password) < 12 or len(password) > 256:
                    return self.json_response(400, {"error": "Use a password between 12 and 256 characters."})
                salt, digest = password_record(password)
                try:
                    with connect() as db:
                        cur = db.execute("INSERT INTO users(email,display_name,role,password_hash,password_salt,created_at) VALUES(?,?,?,?,?,?)", (email, name, role, digest, salt, now_iso()))
                        uid = cur.lastrowid
                except sqlite3.IntegrityError:
                    return self.json_response(409, {"error": "An account with that email already exists."})
                self.delete_session(self.session_token())
                token, csrf = self.new_session(uid)
                with connect() as db: user = db.execute("SELECT id,email,display_name,role FROM users WHERE id=?", (uid,)).fetchone()
                return self.json_response(201, {"user": {"id":user["id"],"email":user["email"],"name":user["display_name"],"role":user["role"]}, "csrf": csrf}, set_cookie=token)
            # Sign-in: perform a dummy PBKDF2 for unknown accounts to reduce timing leaks.
            with connect() as db: user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if user:
                valid = password_matches(password, user["password_salt"], user["password_hash"])
            else:
                password_matches(password, "00112233445566778899aabbccddeeff", "0" * 64)
                valid = False
            if not valid:
                return self.json_response(401, {"error": "Email or password is incorrect."})
            self.delete_session(self.session_token())
            token, csrf = self.new_session(user["id"])
            return self.json_response(200, {"user": {"id":user["id"],"email":user["email"],"name":user["display_name"],"role":user["role"]}, "csrf": csrf}, set_cookie=token)

        s = self.require_auth()
        if not s: return
        if not self.check_csrf(s): return
        if path == "/api/logout":
            self.delete_session(self.session_token())
            return self.json_response(200, {"ok": True}, clear_cookie=True)
        if path == "/api/marks":
            try: d = self.read_json()
            except ValueError as e: return self.json_response(400, {"error": str(e)})
            subject = str(d.get("subject", "")).strip()[:80]
            assessment = str(d.get("assessment", "Other")).strip()[:40]
            try:
                score, maximum = float(d.get("score")), float(d.get("maximum"))
                confidence = int(d.get("confidence", 3))
            except (TypeError, ValueError): return self.json_response(400, {"error": "Enter numeric marks, maximum, and confidence."})
            date = str(d.get("date", ""))
            if not subject or not math.isfinite(score) or not math.isfinite(maximum) or maximum <= 0 or score < 0 or score > maximum or confidence not in range(1,6):
                return self.json_response(400, {"error": "Check subject, marks range, and confidence."})
            try: datetime.strptime(date, "%Y-%m-%d")
            except ValueError: return self.json_response(400, {"error": "Choose a valid assessment date."})
            with connect() as db:
                cur = db.execute("INSERT INTO marks(user_id,subject,assessment,score,maximum,confidence,assessment_date,created_at) VALUES(?,?,?,?,?,?,?,?)", (s["uid"],subject,assessment,score,maximum,confidence,date,now_iso()))
            return self.json_response(201, {"id":cur.lastrowid,"ok":True})
        if path == "/api/plan":
            try: d = self.read_json(); hours=float(d.get("hours")); days=int(d.get("days"))
            except (ValueError,TypeError): return self.json_response(400, {"error": "Enter valid study hours and days."})
            if not math.isfinite(hours) or not 0.5 <= hours <= 12 or days not in (4,5,6,7):
                return self.json_response(400, {"error": "Choose 0.5–12 hours and 4–7 study days per week."})
            with connect() as db:
                marks = db.execute("SELECT subject,score,maximum FROM marks WHERE user_id=?", (s["uid"],)).fetchall()
            groups={}
            for m in marks: groups.setdefault(m["subject"],[]).append(100*m["score"]/m["maximum"])
            ordered=sorted(((name,round(statistics.mean(vals))) for name,vals in groups.items()),key=lambda x:x[1])
            priorities=[x[0] for x in ordered if x[1]<75][:2]
            main=priorities[0] if priorities else (ordered[0][0] if ordered else "your priority subject")
            second=priorities[1] if len(priorities)>1 else next((x[0] for x in ordered if x[0]!=main),"revision")
            mins=round(hours*60)
            blocks=[{"title":"Core concepts — "+main,"minutes":round(mins*.35),"goal":"Review one concept, then explain it from memory."},{"title":"Practice — "+main,"minutes":round(mins*.3),"goal":"Solve questions without notes and record mistakes."},{"title":"Revision — "+second,"minutes":round(mins*.2),"goal":"Use recall prompts, short notes, or a mini-quiz."},{"title":"Review and plan","minutes":max(10,round(mins*.15)),"goal":"Review errors and set tomorrow’s target."}]
            weekday=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
            plan={"hours":hours,"days":days,"blocks":blocks,"schedule":[{"day":weekday[i],**blocks[i%len(blocks)]} for i in range(days)],"updated_at":now_iso()}
            with connect() as db:
                db.execute("INSERT INTO plans(user_id,plan_json,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET plan_json=excluded.plan_json,updated_at=excluded.updated_at", (s["uid"],json.dumps(plan,ensure_ascii=False),plan["updated_at"]))
            return self.json_response(200, {"plan":plan})
        if path == "/api/predict":
            try: d = self.read_json()
            except ValueError as e: return self.json_response(400, {"error": str(e)})
            try:
                x = [float(d[k]) for k in ("attendance","assignments","study_hours","consistency","confidence")]
            except (KeyError,TypeError,ValueError): return self.json_response(400, {"error": "Enter all five learning context values."})
            if not all(math.isfinite(v) for v in x) or not (0<=x[0]<=100 and 0<=x[1]<=100 and 0<=x[2]<=24 and 1<=x[3]<=5 and 1<=x[4]<=5):
                return self.json_response(400, {"error": "One or more learning context values are outside the allowed range."})
            with connect() as db:
                model_row = db.execute("SELECT model_json,rows_total,mae,rmse,r2,baseline_mae,trained_at,is_synthetic FROM active_model WHERE singleton=1").fetchone()
            if not model_row: return self.json_response(409, {"error": "No evaluated model has been trained yet. Ask a provisioned mentor to evaluate an approved dataset."})
            model = json.loads(model_row["model_json"])
            scaled = [1.0] + [(x[i] - model["means"][i]) / model["stds"][i] for i in range(len(x))]
            estimate = max(0.0,min(100.0,sum(a*b for a,b in zip(model["coefficients"],scaled))))
            return self.json_response(200, {"score": round(estimate,1), "is_synthetic": bool(model_row["is_synthetic"]), "metrics": {"mae":model_row["mae"],"rmse":model_row["rmse"],"r2":model_row["r2"],"baseline_mae":model_row["baseline_mae"],"rows_total":model_row["rows_total"],"trained_at":model_row["trained_at"]}, "disclaimer":"An experimental estimate from an evaluated but not necessarily representative dataset. It is not a guarantee or a basis for high-impact decisions."})
        if path == "/api/chat/messages":
            try: d = self.read_json(); other = int(d.get("contact_id", 0))
            except (ValueError, TypeError) as e: return self.json_response(400, {"error": str(e) or "Choose a chat contact."})
            if not self.valid_contact(s, other): return
            body = str(d.get("body", "")).strip()
            if not body or len(body) > 2000: return self.json_response(400, {"error": "Message must contain 1–2,000 characters."})
            key = conversation_key(s["uid"], other)
            with connect() as db:
                last = db.execute("SELECT created_at FROM messages WHERE conversation_key=? AND sender_id=? ORDER BY id DESC LIMIT 1", (key,s["uid"])).fetchone()
                if last:
                    last_dt = datetime.fromisoformat(last["created_at"])
                    if (datetime.now(timezone.utc)-last_dt).total_seconds() < 0.45:
                        return self.json_response(429, {"error": "Please wait a moment before sending another message."})
                cur = db.execute("INSERT INTO messages(conversation_key,sender_id,recipient_id,body,created_at) VALUES(?,?,?,?,?)", (key,s["uid"],other,body,now_iso()))
                row = db.execute("SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.display_name AS sender_name FROM messages m JOIN users u ON u.id=m.sender_id WHERE m.id=?", (cur.lastrowid,)).fetchone()
            return self.json_response(201, {"message":dict(row)})
        if path == "/api/evaluate":
            if s["role"] != "mentor": return self.json_response(403, {"error": "Model evaluation is available to mentor accounts."})
            try: d = self.read_json()
            except ValueError as e: return self.json_response(400, {"error": str(e)})
            dataset = str(d.get("dataset", "")).strip()
            try: result = evaluate_csv(dataset)
            except ValueError as e: return self.json_response(400, {"error": str(e)})
            name = str(d.get("name", "Uploaded dataset")).strip()[:100] or "Uploaded dataset"
            trained_at = now_iso()
            with connect() as db:
                cur = db.execute("INSERT INTO evaluations(user_id,dataset_name,rows_total,mae,rmse,r2,baseline_mae,created_at) VALUES(?,?,?,?,?,?,?,?)", (s["uid"],name,result["rows_total"],result["mae"],result["rmse"],result["r2"],result["baseline_mae"],now_iso()))
                db.execute("INSERT INTO active_model(singleton,owner_id,model_json,rows_total,mae,rmse,r2,baseline_mae,trained_at,is_synthetic) VALUES(1,?,?,?,?,?,?,?,?,0) ON CONFLICT(singleton) DO UPDATE SET owner_id=excluded.owner_id,model_json=excluded.model_json,rows_total=excluded.rows_total,mae=excluded.mae,rmse=excluded.rmse,r2=excluded.r2,baseline_mae=excluded.baseline_mae,trained_at=excluded.trained_at,is_synthetic=0", (s["uid"],json.dumps(result["model"],separators=(",",":")),result["rows_total"],result["mae"],result["rmse"],result["r2"],result["baseline_mae"],trained_at))
            result.pop("model",None)
            result["id"] = cur.lastrowid; result["dataset_name"] = name; result["created_at"] = trained_at
            return self.json_response(201, result)
        return self.json_response(404, {"error": "Not found."})

    def do_DELETE(self):
        path = urlparse(self.path).path
        s = self.require_auth()
        if not s: return
        if not self.check_csrf(s): return
        if path.startswith("/api/marks/"):
            try: mark_id = int(path.rsplit("/",1)[1])
            except ValueError: return self.json_response(400, {"error": "Invalid mark ID."})
            with connect() as db:
                cur = db.execute("DELETE FROM marks WHERE id=? AND user_id=?", (mark_id,s["uid"]))
            if cur.rowcount == 0: return self.json_response(404, {"error": "Mark not found."})
            return self.json_response(200, {"ok": True})
        return self.json_response(404, {"error": "Not found."})


def main():
    parser = argparse.ArgumentParser(description="Run the local SARATHI student portal")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--secure-cookies", action="store_true", help="Set Secure cookie flag when behind HTTPS")
    parser.add_argument("--create-mentor", action="store_true", help="Provision a mentor account locally, then exit")
    args = parser.parse_args()
    init_db()
    if args.create_mentor:
        email = input("Mentor email: ").strip().lower()
        name = input("Mentor display name: ").strip()[:60]
        password = getpass.getpass("Set password (12+ characters): ")
        if len(name) < 2 or "@" not in email or email.endswith("@sarathi.invalid") or len(password) < 12:
            raise SystemExit("A valid email, 2+ character name, and 12+ character password are required.")
        salt, digest = password_record(password)
        try:
            with connect() as db:
                db.execute("INSERT INTO users(email,display_name,role,password_hash,password_salt,created_at) VALUES(?,?,?,?,?,?)", (email,name,"mentor",digest,salt,now_iso()))
        except sqlite3.IntegrityError:
            raise SystemExit("That email is already registered.")
        print("Mentor account created. The account can now sign in and chat with students.")
        return
    server = SarathiServer((args.host,args.port),Handler,secure_cookies=args.secure_cookies)
    print("SARATHI is listening at http://%s:%d" % (args.host,args.port))
    print("Local demo only. Put behind HTTPS with an authenticated reverse proxy before any network deployment.")
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nStopping SARATHI.")
    finally: server.server_close()

if __name__ == "__main__": main()

