import hashlib
import hmac
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class CoopAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "coop.sqlite3"
        self.storage = root / "storage"
        self.patches = [
            patch.object(app, "DATABASE_PATH", self.db_path),
            patch.object(app, "STORAGE_DIR", self.storage),
            patch.object(app, "UPLOAD_DIR", self.storage / "uploads"),
            patch.dict("os.environ", {"ADMIN_TELEGRAM_IDS": "999"}),
        ]
        for item in self.patches:
            item.start()
        app.initialize_database()
        app.remember_user({"id": 123, "username": "worker", "first_name": "Worker"})
        app.enroll_employee(123, 250000)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_only_first_approved_report_credits_salary_in_utc_month(self):
        report_a = app.create_report(123, "First work report")
        report_b = app.create_report(123, "Another report")
        with patch.object(app, "utc_now", return_value="2026-11-13T10:00:00+00:00"):
            first = app.decide_report(report_a, "approved")
            second = app.decide_report(report_b, "approved")
        self.assertEqual(first, {"credited_cents": 250000, "period": "2026-11"})
        self.assertEqual(second, {"credited_cents": 0, "period": "2026-11"})
        self.assertEqual(app.summary_for(123)["balance_cents"], 250000)

    def test_report_decision_is_final_and_rejection_does_not_credit(self):
        report_id = app.create_report(123, "Work done")
        rejected = app.decide_report(report_id, "rejected", "Needs more detail")
        self.assertEqual(rejected["credited_cents"], 0)
        with self.assertRaises(app.APIError) as error:
            app.decide_report(report_id, "approved")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(app.summary_for(123)["balance_cents"], 0)

    def test_wallet_requires_trc20_address(self):
        with self.assertRaises(ValueError):
            app.set_wallet(123, "not-a-wallet")

    def test_init_data_signature_and_age_are_checked(self):
        token = "test-token"
        now = 1_800_000_000
        values = {
            "auth_date": str(now),
            "user": json.dumps({"id": 321, "username": "tester"}, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret_key, check.encode(), hashlib.sha256).hexdigest()
        encoded = urllib.parse.urlencode(values)
        self.assertEqual(app.validate_init_data(encoded, token, now), {"id": 321, "username": "tester"})
        self.assertIsNone(app.validate_init_data(encoded, token, now + 86401))
        self.assertIsNone(app.validate_init_data(encoded + "&user=%7B%7D", token, now))

    def test_api_requires_signed_telegram_identity_and_admin_role(self):
        token = "test-token"
        user = {"id": 123, "username": "worker"}
        values = {
            "auth_date": str(int(time.time())),
            "user": json.dumps(user, separators=(",", ":")),
        }
        check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        values["hash"] = hmac.new(secret_key, check.encode(), hashlib.sha256).hexdigest()
        init_data = urllib.parse.urlencode(values)
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/admin/reports"
        try:
            with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                urllib.request.urlopen(url, timeout=3)
            self.assertEqual(unauthenticated.exception.code, 401)
            unauthenticated.exception.close()
            with patch.object(app, "BOT_TOKEN", token):
                request = urllib.request.Request(
                    url, headers={"X-Telegram-Init-Data": init_data}
                )
                with self.assertRaises(urllib.error.HTTPError) as non_admin:
                    urllib.request.urlopen(request, timeout=3)
            self.assertEqual(non_admin.exception.code, 403)
            non_admin.exception.close()
            admin_values = {
                "auth_date": str(int(time.time())),
                "user": json.dumps({"id": 999}, separators=(",", ":")),
            }
            admin_check = "\n".join(
                f"{key}={value}" for key, value in sorted(admin_values.items())
            )
            admin_values["hash"] = hmac.new(
                secret_key, admin_check.encode(), hashlib.sha256
            ).hexdigest()
            credit_url = f"http://127.0.0.1:{server.server_port}/api/admin/employees/123/credit"
            credit_request = urllib.request.Request(
                credit_url,
                data=json.dumps(
                    {"amount": "12.50", "description": "Manual bonus"}
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Telegram-Init-Data": urllib.parse.urlencode(admin_values),
                },
                method="POST",
            )
            with patch.object(app, "BOT_TOKEN", token):
                with urllib.request.urlopen(credit_request, timeout=3) as response:
                    self.assertEqual(response.status, 200)
            self.assertEqual(app.summary_for(123)["balance_cents"], 1250)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_manual_payout_cannot_exceed_internal_balance(self):
        report_id = app.create_report(123, "Monthly report")
        app.decide_report(report_id, "approved")
        with self.assertRaises(app.APIError):
            app.record_payout(123, 250001)
        app.record_payout(123, 50000, "manual TXID")
        self.assertEqual(app.summary_for(123)["balance_cents"], 200000)

    def test_admin_can_credit_balance_without_report_or_employee_confirmation(self):
        app.credit_balance(123, 7250, "One-time bonus")
        app.credit_balance(123, 300, "")
        summary = app.summary_for(123)
        self.assertEqual(summary["balance_cents"], 7550)
        self.assertEqual(summary["ledger"][0]["kind"], "credit")
        self.assertEqual(summary["ledger"][0]["description"], "Admin balance credit")
        self.assertEqual(summary["ledger"][1]["description"], "One-time bonus")

    def test_ledger_schema_migrates_existing_entries_for_manual_credits(self):
        with app.connect_db() as db:
            db.execute("DROP INDEX ledger_employee_created")
            db.execute("ALTER TABLE ledger RENAME TO ledger_previous")
            db.execute(
                """CREATE TABLE ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES employees(telegram_id),
                    kind TEXT NOT NULL CHECK (kind IN ('salary', 'payout')),
                    amount_cents INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    report_id INTEGER REFERENCES reports(id),
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """INSERT INTO ledger (telegram_id, kind, amount_cents, description, created_at)
                   VALUES (123, 'salary', 1500, 'Legacy salary', ?)""",
                (app.utc_now(),),
            )
            db.execute("DROP TABLE ledger_previous")
            db.execute("CREATE INDEX ledger_employee_created ON ledger(telegram_id, created_at)")
        app.initialize_database()
        app.credit_balance(123, 250, "New credit")
        summary = app.summary_for(123)
        self.assertEqual(summary["balance_cents"], 1750)
        self.assertEqual({entry["kind"] for entry in summary["ledger"]}, {"salary", "credit"})


if __name__ == "__main__":
    unittest.main()
