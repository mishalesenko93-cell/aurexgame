"""AUREX Company collaboration bot and Telegram Mini App."""

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", ROOT / ".data" / "coop.sqlite3"))
STORAGE_DIR = Path(os.environ.get("STORAGE_DIR", ROOT / ".data"))
UPLOAD_DIR = STORAGE_DIR / "uploads"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp", "txt", "csv", "zip"}
TRC20_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
LOGGER = logging.getLogger("aurex-coop")


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def admin_ids():
    raw = os.environ.get("ADMIN_TELEGRAM_IDS", "")
    return {int(value.strip()) for value in raw.split(",") if value.strip().isdigit()}


@contextmanager
def connect_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                wallet TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                kind TEXT NOT NULL CHECK (kind IN ('job', 'collaboration')),
                position TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'accepted', 'rejected')),
                decision_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS employees (
                telegram_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                monthly_salary_cents INTEGER NOT NULL CHECK (monthly_salary_cents > 0),
                currency TEXT NOT NULL DEFAULT 'USDT',
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES employees(telegram_id),
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
                decision_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS report_files (
                id TEXT PRIMARY KEY,
                report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS salary_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES employees(telegram_id),
                period TEXT NOT NULL,
                report_id INTEGER NOT NULL REFERENCES reports(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL,
                UNIQUE (telegram_id, period)
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL REFERENCES employees(telegram_id),
                kind TEXT NOT NULL CHECK (kind IN ('salary', 'payout')),
                amount_cents INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                report_id INTEGER REFERENCES reports(id),
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS applications_status_created
                ON applications(status, created_at);
            CREATE INDEX IF NOT EXISTS reports_status_created ON reports(status, created_at);
            CREATE INDEX IF NOT EXISTS ledger_employee_created
                ON ledger(telegram_id, created_at);
            """
        )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def period_for(timestamp=None):
    value = timestamp or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m")


def validate_init_data(init_data, bot_token=None, now=None, max_age=86400):
    """Return the signed Telegram WebApp user or None for invalid/stale data."""
    token = BOT_TOKEN if bot_token is None else bot_token
    try:
        parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        if len({key for key, _ in parsed}) != len(parsed):
            return None
        values = dict(parsed)
        got_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        current = time.time() if now is None else now
        if auth_date > current + 30 or current - auth_date > max_age:
            return None
        check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, got_hash):
            return None
        user = json.loads(values["user"])
        if not isinstance(user.get("id"), int):
            return None
        return user
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def remember_user(user):
    now = utc_now()
    with connect_db() as db:
        db.execute(
            """INSERT INTO users (telegram_id, username, first_name, last_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 username=excluded.username, first_name=excluded.first_name,
                 last_name=excluded.last_name, updated_at=excluded.updated_at""",
            (
                user["id"],
                (user.get("username") or "")[:64],
                (user.get("first_name") or "")[:64],
                (user.get("last_name") or "")[:64],
                now,
                now,
            ),
        )


def money_to_cents(value):
    if isinstance(value, bool):
        raise ValueError("Invalid amount")
    try:
        amount = round(float(value) * 100)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid amount")
    if amount <= 0 or amount > 100_000_000_000:
        raise ValueError("Amount must be greater than zero")
    if abs(amount / 100 - float(value)) > 0.000001:
        raise ValueError("Amount may have at most two decimal places")
    return amount


def display_user(row):
    name = " ".join(part for part in (row["first_name"], row["last_name"]) if part).strip()
    return {
        "telegram_id": row["telegram_id"],
        "username": row["username"],
        "name": name or row["username"] or str(row["telegram_id"]),
    }


def set_wallet(telegram_id, wallet):
    wallet = wallet.strip()
    if wallet and not TRC20_RE.fullmatch(wallet):
        raise ValueError("Enter a valid TRC20 address starting with T")
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE users SET wallet=?, updated_at=? WHERE telegram_id=?",
            (wallet, utc_now(), telegram_id),
        )
        if cursor.rowcount != 1:
            raise APIError(404, "User profile not found")


def submit_application(telegram_id, kind, position, message):
    if kind not in {"job", "collaboration"}:
        raise ValueError("Choose job or collaboration")
    message = message.strip()
    position = position.strip()[:120]
    if not message or len(message) > 4000:
        raise ValueError("Message must be between 1 and 4000 characters")
    with connect_db() as db:
        if db.execute(
            "SELECT 1 FROM applications WHERE telegram_id=? AND status='pending'",
            (telegram_id,),
        ).fetchone():
            raise APIError(409, "You already have an application under review")
        cursor = db.execute(
            """INSERT INTO applications (telegram_id, kind, position, message, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (telegram_id, kind, position, message, utc_now()),
        )
        return cursor.lastrowid


def enroll_employee(telegram_id, salary_cents):
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM users WHERE telegram_id=?", (telegram_id,)).fetchone():
            raise APIError(404, "User must start the bot before enrollment")
        db.execute(
            """INSERT INTO employees (telegram_id, monthly_salary_cents, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(telegram_id) DO UPDATE SET
                 monthly_salary_cents=excluded.monthly_salary_cents, active=1""",
            (telegram_id, salary_cents, now),
        )


def create_report(telegram_id, message):
    message = message.strip()
    if not message or len(message) > 4000:
        raise ValueError("Report must be between 1 and 4000 characters")
    with connect_db() as db:
        if not db.execute(
            "SELECT 1 FROM employees WHERE telegram_id=? AND active=1", (telegram_id,)
        ).fetchone():
            raise APIError(403, "Only active employees can submit reports")
        cursor = db.execute(
            "INSERT INTO reports (telegram_id, message, created_at) VALUES (?, ?, ?)",
            (telegram_id, message, utc_now()),
        )
        return cursor.lastrowid


def decide_report(report_id, decision, note=""):
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected")
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        report = db.execute(
            """SELECT r.*, e.monthly_salary_cents, e.active
               FROM reports r JOIN employees e USING (telegram_id) WHERE r.id=?""",
            (report_id,),
        ).fetchone()
        if not report:
            raise APIError(404, "Report not found")
        if report["status"] != "pending":
            raise APIError(409, "Report has already been reviewed")
        db.execute(
            "UPDATE reports SET status=?, decision_note=?, decided_at=? WHERE id=?",
            (decision, note.strip()[:1000], now, report_id),
        )
        if decision == "approved" and report["active"]:
            period = period_for(now)
            inserted = db.execute(
                """INSERT OR IGNORE INTO salary_credits
                   (telegram_id, period, report_id, amount_cents, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (report["telegram_id"], period, report_id, report["monthly_salary_cents"], now),
            )
            if inserted.rowcount:
                db.execute(
                    """INSERT INTO ledger
                       (telegram_id, kind, amount_cents, description, report_id, created_at)
                       VALUES (?, 'salary', ?, ?, ?, ?)""",
                    (
                        report["telegram_id"],
                        report["monthly_salary_cents"],
                        f"Monthly salary {period}",
                        report_id,
                        now,
                    ),
                )
                return {"credited_cents": report["monthly_salary_cents"], "period": period}
        return {"credited_cents": 0, "period": period_for(now)}


def record_payout(telegram_id, amount_cents, reference=""):
    now = utc_now()
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        employee = db.execute(
            "SELECT active FROM employees WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        if not employee or not employee["active"]:
            raise APIError(404, "Active employee not found")
        balance = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()[0]
        if amount_cents > balance:
            raise APIError(409, "Payout exceeds the available balance")
        db.execute(
            """INSERT INTO ledger
               (telegram_id, kind, amount_cents, description, created_at)
               VALUES (?, 'payout', ?, ?, ?)""",
            (telegram_id, -amount_cents, reference.strip()[:200] or "Manual USDT payout", now),
        )


def safe_original_filename(filename):
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
    filename = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip(" .")
    filename = filename[:120]
    if not filename or filename in {".", ".."}:
        raise ValueError("Invalid file name")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Allowed file types: PDF, PNG, JPG, WEBP, TXT, CSV, ZIP")
    return filename, extension


def valid_file_content(extension, data):
    signatures = {
        "pdf": data.startswith(b"%PDF-"),
        "png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpg": data.startswith(b"\xff\xd8\xff"),
        "jpeg": data.startswith(b"\xff\xd8\xff"),
        "webp": len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
        "zip": data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"),
        "txt": False,
        "csv": False,
    }
    if extension in {"txt", "csv"}:
        try:
            data.decode("utf-8")
            return b"\x00" not in data
        except UnicodeDecodeError:
            return False
    return signatures.get(extension, False)


def attach_file(report_id, telegram_id, filename, data):
    safe_name, extension = safe_original_filename(filename)
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("File must be between 1 byte and 10 MB")
    if not valid_file_content(extension, data):
        raise ValueError("File content does not match the selected file type")
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    file_id = secrets.token_hex(16)
    path = UPLOAD_DIR / stored_name
    with connect_db() as db:
        report = db.execute(
            "SELECT telegram_id, status FROM reports WHERE id=?", (report_id,)
        ).fetchone()
        if not report:
            raise APIError(404, "Report not found")
        if report["telegram_id"] != telegram_id and telegram_id not in admin_ids():
            raise APIError(403, "You cannot attach a file to this report")
        if report["status"] != "pending":
            raise APIError(409, "Files can only be added to pending reports")
        path.write_bytes(data)
        try:
            db.execute(
                """INSERT INTO report_files
                   (id, report_id, original_name, stored_name, content_type, size_bytes, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    file_id,
                    report_id,
                    safe_name,
                    stored_name,
                    mimetypes.guess_type(safe_name)[0] or "application/octet-stream",
                    len(data),
                    utc_now(),
                ),
            )
        except Exception:
            path.unlink(missing_ok=True)
            raise
    return file_id


def summary_for(telegram_id):
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE telegram_id=?", (telegram_id,)).fetchone()
        employee = db.execute(
            "SELECT * FROM employees WHERE telegram_id=? AND active=1", (telegram_id,)
        ).fetchone()
        balance = db.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM ledger WHERE telegram_id=?",
            (telegram_id,),
        ).fetchone()[0]
        ledger = db.execute(
            """SELECT kind, amount_cents, description, created_at
               FROM ledger WHERE telegram_id=? ORDER BY id DESC LIMIT 20""",
            (telegram_id,),
        ).fetchall()
        reports = db.execute(
            "SELECT * FROM reports WHERE telegram_id=? ORDER BY id DESC LIMIT 20",
            (telegram_id,),
        ).fetchall()
        applications = db.execute(
            "SELECT * FROM applications WHERE telegram_id=? ORDER BY id DESC LIMIT 10",
            (telegram_id,),
        ).fetchall()
        report_ids = [report["id"] for report in reports]
        files = {}
        if report_ids:
            placeholders = ",".join("?" for _ in report_ids)
            for row in db.execute(
                f"SELECT * FROM report_files WHERE report_id IN ({placeholders})",
                report_ids,
            ):
                files.setdefault(row["report_id"], []).append(
                    {"id": row["id"], "name": row["original_name"], "size": row["size_bytes"]}
                )
    return {
        "user": display_user(user),
        "wallet": user["wallet"],
        "employee": bool(employee),
        "monthly_salary_cents": employee["monthly_salary_cents"] if employee else 0,
        "balance_cents": balance,
        "ledger": [dict(row) for row in ledger],
        "reports": [
            {**dict(row), "files": files.get(row["id"], [])} for row in reports
        ],
        "applications": [dict(row) for row in applications],
    }


def telegram_api(method, **params):
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))
    return result["result"]


def notify_admins(text):
    for telegram_id in admin_ids():
        try:
            telegram_api("sendMessage", chat_id=telegram_id, text=text)
        except (URLError, RuntimeError, TimeoutError):
            LOGGER.exception("Could not notify admin %s", telegram_id)


def handle_telegram_update(update):
    message = update.get("message") or {}
    user = message.get("from") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if not chat_id:
        return
    text = (message.get("text") or "").strip()
    if text.startswith("/start"):
        remember_user(user)
        telegram_api(
            "sendMessage",
            chat_id=chat_id,
            text=(
                "Добро пожаловать в AUREX Company — пространство для заявок, "
                "рабочих отчётов и выплат.\n\n"
                "Откройте Mini App, чтобы подать заявку или перейти в кабинет."
            ),
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Открыть кабинет AUREX", "web_app": {"url": WEBAPP_URL}}]
                ]
            },
        )


def poll_telegram():
    offset = 0
    while True:
        try:
            updates = telegram_api(
                "getUpdates", offset=offset, timeout=30, allowed_updates=["message"]
            )
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_telegram_update(update)
                except Exception:
                    LOGGER.exception("Telegram update handler failed")
        except Exception:
            LOGGER.exception("Telegram long polling failed")
            time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    server_version = "AUREXCoop/1.0"

    def send_json(self, status, body):
        encoded = b"" if status == 204 else json.dumps(
            body, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        origin = os.environ.get("ALLOWED_ORIGIN", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        if status != 204:
            self.wfile.write(encoded)

    def send_file(self, path, filename, content_type):
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename.replace(chr(34), "")}"',
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with path.open("rb") as source:
            while True:
                block = source.read(64 * 1024)
                if not block:
                    break
                self.wfile.write(block)

    def get_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "Invalid content length")
        if length < 0 or length > MAX_JSON_BYTES:
            raise APIError(413, "Request is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise APIError(400, "Invalid JSON")
        if not isinstance(value, dict):
            raise APIError(400, "Expected a JSON object")
        return value

    def authenticated_user(self, data=None):
        init_data = (
            self.headers.get("X-Telegram-Init-Data", "")
            if data is None
            else data.get("initData", "")
        )
        user = validate_init_data(init_data)
        if not user:
            raise APIError(401, "Telegram authentication failed; reopen the Mini App")
        remember_user(user)
        return user

    def require_admin(self, user):
        if user["id"] not in admin_ids():
            raise APIError(403, "Admin access required")

    def do_OPTIONS(self):
        self.send_json(204, {})

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/index.html"}:
            data = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/health":
            return self.send_json(200, {"ok": True, "service": "AUREX Company"})
        try:
            user = self.authenticated_user()
            if path == "/api/me":
                return self.send_json(
                    200,
                    {**summary_for(user["id"]), "is_admin": user["id"] in admin_ids()},
                )
            if path == "/api/admin/users":
                self.require_admin(user)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get(
                    "q", [""]
                )[0].strip().lstrip("@").lower()
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT u.*, CASE WHEN e.telegram_id IS NULL THEN 0 ELSE 1 END AS employee,
                                  e.monthly_salary_cents, COALESCE(b.balance_cents, 0) AS balance_cents
                           FROM users u LEFT JOIN employees e USING (telegram_id)
                           LEFT JOIN (
                             SELECT telegram_id, SUM(amount_cents) AS balance_cents
                             FROM ledger GROUP BY telegram_id
                           ) b USING (telegram_id)
                           WHERE lower(u.username) LIKE ? OR lower(u.first_name) LIKE ?
                              OR CAST(u.telegram_id AS TEXT) LIKE ?
                           ORDER BY u.updated_at DESC LIMIT 50""",
                        (f"%{query}%", f"%{query}%", f"%{query}%"),
                    ).fetchall()
                return self.send_json(
                    200,
                    [
                        {
                            "user": display_user(row),
                            "employee": bool(row["employee"]),
                            "salary_cents": row["monthly_salary_cents"] or 0,
                            "balance_cents": row["balance_cents"],
                            "wallet": row["wallet"],
                        }
                        for row in rows
                    ],
                )
            if path == "/api/admin/applications":
                self.require_admin(user)
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT a.*, u.username, u.first_name, u.last_name
                           FROM applications a JOIN users u USING (telegram_id)
                           ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.id DESC
                           LIMIT 100"""
                    ).fetchall()
                return self.send_json(200, [dict(row) for row in rows])
            if path == "/api/admin/reports":
                self.require_admin(user)
                with connect_db() as db:
                    rows = db.execute(
                        """SELECT r.*, u.username, u.first_name, u.last_name
                           FROM reports r JOIN users u USING (telegram_id)
                           ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.id DESC
                           LIMIT 100"""
                    ).fetchall()
                    result = []
                    for row in rows:
                        item = dict(row)
                        item["files"] = [
                            dict(file)
                            for file in db.execute(
                                "SELECT id, original_name, size_bytes FROM report_files WHERE report_id=?",
                                (row["id"],),
                            )
                        ]
                        result.append(item)
                return self.send_json(200, result)
            file_match = re.fullmatch(r"/api/files/([a-f0-9]{32})", path)
            if file_match:
                with connect_db() as db:
                    file_row = db.execute(
                        """SELECT f.*, r.telegram_id FROM report_files f
                           JOIN reports r USING (report_id) WHERE f.id=?""",
                        (file_match.group(1),),
                    ).fetchone()
                if not file_row:
                    raise APIError(404, "File not found")
                if (
                    file_row["telegram_id"] != user["id"]
                    and user["id"] not in admin_ids()
                ):
                    raise APIError(403, "You cannot access this file")
                if not re.fullmatch(r"[a-f0-9]{32}\.(pdf|png|jpg|jpeg|webp|txt|csv|zip)", file_row["stored_name"]):
                    raise APIError(404, "File not found")
                path_on_disk = UPLOAD_DIR / file_row["stored_name"]
                if not path_on_disk.is_file():
                    raise APIError(404, "File not found")
                return self.send_file(
                    path_on_disk, file_row["original_name"], file_row["content_type"]
                )
            raise APIError(404, "Not found")
        except APIError as error:
            self.send_json(error.status, {"error": error.message})
        except Exception:
            LOGGER.exception("GET request failed")
            self.send_json(500, {"error": "Internal server error"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/report-files":
                user = self.authenticated_user()
                self.upload_report_file(user)
                return
            data = self.get_json()
            user = self.authenticated_user(data)
            uid = user["id"]
            if path == "/api/wallet":
                set_wallet(uid, str(data.get("wallet", "")))
                return self.send_json(200, {"ok": True})
            if path == "/api/applications":
                application_id = submit_application(
                    uid,
                    data.get("kind"),
                    str(data.get("position", "")),
                    str(data.get("message", "")),
                )
                notify_admins(
                    f"Новая заявка AUREX #{application_id} "
                    f"({'работа' if data.get('kind') == 'job' else 'сотрудничество'}) от "
                    f"@{user.get('username') or uid}"
                )
                return self.send_json(201, {"id": application_id})
            if path == "/api/reports":
                report_id = create_report(uid, str(data.get("message", "")))
                notify_admins(
                    f"Новый рабочий отчёт AUREX #{report_id} от "
                    f"@{user.get('username') or uid}"
                )
                return self.send_json(201, {"id": report_id})
            application_match = re.fullmatch(r"/api/admin/applications/(\d+)/decision", path)
            if application_match:
                self.require_admin(user)
                self.decide_application(int(application_match.group(1)), data)
                return self.send_json(200, {"ok": True})
            employee_match = re.fullmatch(r"/api/admin/employees/(\d+)", path)
            if employee_match:
                self.require_admin(user)
                salary = money_to_cents(data.get("monthly_salary"))
                enroll_employee(int(employee_match.group(1)), salary)
                return self.send_json(200, {"ok": True})
            report_match = re.fullmatch(r"/api/admin/reports/(\d+)/decision", path)
            if report_match:
                self.require_admin(user)
                decision = data.get("decision")
                result = decide_report(
                    int(report_match.group(1)), decision, str(data.get("note", ""))
                )
                return self.send_json(
                    200,
                    {
                        "ok": True,
                        "credited_cents": result["credited_cents"],
                        "period": result["period"],
                    },
                )
            payout_match = re.fullmatch(r"/api/admin/employees/(\d+)/payout", path)
            if payout_match:
                self.require_admin(user)
                record_payout(
                    int(payout_match.group(1)),
                    money_to_cents(data.get("amount")),
                    str(data.get("reference", "")),
                )
                return self.send_json(200, {"ok": True})
            raise APIError(404, "Not found")
        except APIError as error:
            self.send_json(error.status, {"error": error.message})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except Exception:
            LOGGER.exception("POST request failed")
            self.send_json(500, {"error": "Internal server error"})

    def decide_application(self, application_id, data):
        decision = data.get("decision")
        if decision not in {"accepted", "rejected"}:
            raise ValueError("Decision must be accepted or rejected")
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            application = db.execute(
                "SELECT * FROM applications WHERE id=?", (application_id,)
            ).fetchone()
            if not application:
                raise APIError(404, "Application not found")
            if application["status"] != "pending":
                raise APIError(409, "Application has already been reviewed")
            db.execute(
                """UPDATE applications SET status=?, decision_note=?, decided_at=? WHERE id=?""",
                (
                    decision,
                    str(data.get("note", "")).strip()[:1000],
                    utc_now(),
                    application_id,
                ),
            )

    def upload_report_file(self, user):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "Invalid content length")
        if length <= 0 or length > MAX_UPLOAD_BYTES + 128 * 1024:
            raise APIError(413, "File must be no larger than 10 MB")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise APIError(400, "Expected a multipart file upload")
        raw = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + self.rfile.read(length)
        )
        form = BytesParser(policy=policy.default).parsebytes(raw)
        fields = {}
        for part in form.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                fields["filename"] = part.get_filename()
                fields["data"] = part.get_payload(decode=True) or b""
            elif name == "report_id":
                fields["report_id"] = (part.get_payload(decode=True) or b"").decode(
                    "utf-8", "replace"
                )
        try:
            report_id = int(fields["report_id"])
            filename = fields["filename"]
            file_data = fields["data"]
        except (KeyError, TypeError, ValueError):
            raise APIError(400, "A report ID and file are required")
        file_id = attach_file(report_id, user["id"], filename, file_data)
        self.send_json(201, {"id": file_id})

    def log_message(self, fmt, *args):
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in the service environment")
    if not WEBAPP_URL.startswith("https://"):
        raise SystemExit("Set WEBAPP_URL to the service's public HTTPS address")
    initialize_database()
    try:
        telegram_api(
            "setChatMenuButton",
            menu_button={
                "type": "web_app",
                "text": "Кабинет AUREX",
                "web_app": {"url": WEBAPP_URL},
            },
        )
    except Exception:
        LOGGER.exception("Could not configure Telegram Mini App menu button")
    threading.Thread(target=poll_telegram, daemon=True).start()
    LOGGER.info("AUREX Company service listening on port %s", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
