import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")
import bot


class WinterSeasonTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_winter_file = bot.WINTER_FILE
        self.old_db_file = bot.DB_FILE
        bot.WINTER_FILE = os.path.join(self.temp_dir.name, "winter.json")
        bot.DB_FILE = os.path.join(self.temp_dir.name, "purchases.json")
        self.user = {"id": 12345, "first_name": "Test"}

    def tearDown(self):
        bot.WINTER_FILE = self.old_winter_file
        bot.DB_FILE = self.old_db_file
        self.temp_dir.cleanup()

    def call_api(self, path, payload=None, now=None):
        with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START if now is None else now):
            return bot.winter_api(path, payload or {}, self.user)

    def save_profile(self, now, update):
        data = bot.winter_load()
        profile = bot.winter_profile(data, self.user, now)
        update(profile)
        bot.winter_save(data)

    def test_season_is_hidden_before_start_and_active_at_server_boundary(self):
        start = datetime.fromtimestamp(bot.WINTER_START, timezone.utc)
        end = datetime.fromtimestamp(bot.WINTER_END, timezone.utc)
        self.assertEqual(start, datetime(2026, 11, 30, 22, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2027, 2, 28, 22, tzinfo=timezone.utc))
        status, response = self.call_api("/api/winter/state", now=bot.WINTER_START - 1)
        self.assertEqual(status, 200)
        self.assertFalse(response["active"])
        self.assertNotIn("rewards", response)
        with self.assertRaises(bot.WinterError) as error:
            self.call_api("/api/winter/arena/start", now=bot.WINTER_START - 1)
        self.assertEqual(error.exception.code, "season_not_active")
        _, ended = self.call_api("/api/winter/state", now=bot.WINTER_END)
        self.assertFalse(ended["active"])

        status, response = self.call_api("/api/winter/state", now=bot.WINTER_START)
        self.assertEqual(status, 200)
        self.assertTrue(response["active"])
        self.assertTrue(response["offerAvailable"])
        self.assertEqual(len(response["rewards"]), 50)
        old_user = {"id": 54321, "first_name": "Legacy"}
        with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START + 1):
            _, legacy_state = bot.winter_api("/api/winter/state", {}, old_user)
        self.assertTrue(legacy_state["offerAvailable"])

    def test_offer_dismissal_and_pass_reward_claim_are_persistent_and_idempotent(self):
        _, state = self.call_api("/api/winter/state")
        self.assertTrue(state["offerAvailable"])
        _, state = self.call_api("/api/winter/offer-dismiss", now=bot.WINTER_START + 1)
        self.assertFalse(state["offerAvailable"])

        self.save_profile(bot.WINTER_START + 2, lambda profile: profile.update(xp=100))
        _, first = self.call_api("/api/winter/claim-reward", {"track": "free", "level": 1}, bot.WINTER_START + 3)
        _, duplicate = self.call_api("/api/winter/claim-reward", {"track": "free", "level": 1}, bot.WINTER_START + 4)
        self.assertEqual(first["state"]["inventory"]["snowflakes"], 16)
        self.assertTrue(duplicate["alreadyClaimed"])
        self.assertEqual(duplicate["state"]["inventory"]["snowflakes"], 16)
        with self.assertRaises(bot.WinterError) as error:
            self.call_api("/api/winter/claim-reward", {"track": "premium", "level": 1}, bot.WINTER_START + 5)
        self.assertEqual(error.exception.code, "premium_required")

    def test_task_and_case_claim_retries_do_not_duplicate_rewards(self):
        now = bot.WINTER_START + 10
        task = bot.winter_active_quests(now)["daily"][0]
        self.save_profile(now, lambda profile: profile["daily"].update({task["counter"]: task["goal"]}))
        _, first_task = self.call_api("/api/winter/claim-task", {"taskId": task["id"]}, now + 1)
        _, duplicate_task = self.call_api("/api/winter/claim-task", {"taskId": task["id"]}, now + 2)
        self.assertEqual(first_task["coinsAwarded"], task["coins"])
        self.assertEqual(first_task["state"]["currencyReceipts"][0]["amount"], task["coins"])
        self.assertTrue(duplicate_task["alreadyClaimed"])
        self.assertEqual(duplicate_task["state"]["xp"], task["xp"])
        self.assertEqual(
            duplicate_task["state"]["currencyReceipts"][0]["id"],
            first_task["state"]["currencyReceipts"][0]["id"],
        )
        receipt_id = first_task["state"]["currencyReceipts"][0]["id"]
        _, ack = self.call_api("/api/winter/receipt/ack", {"receiptId": receipt_id}, now + 3)
        self.assertTrue(ack["ok"])
        _, state = self.call_api("/api/winter/state", now=now + 4)
        self.assertEqual(state["currencyReceipts"], [])

        self.save_profile(now + 5, lambda profile: profile["inventory"].update(cases=1))
        request_id = str(uuid.uuid4())
        _, first_case = self.call_api("/api/winter/case/open", {"requestId": request_id}, now + 6)
        _, duplicate_case = self.call_api("/api/winter/case/open", {"requestId": request_id}, now + 7)
        self.assertEqual(first_case["reward"], duplicate_case["reward"])
        self.assertTrue(duplicate_case["alreadyOpened"])
        self.assertEqual(duplicate_case["state"]["inventory"]["cases"], 0)

    def test_daily_and_weekly_quests_rotate_on_kyiv_calendar(self):
        start = bot.WINTER_START
        tomorrow = start + 86400
        next_week = start + 7 * 86400
        first = bot.winter_active_quests(start)
        next_day = bot.winter_active_quests(tomorrow)
        next_week_quests = bot.winter_active_quests(next_week)
        self.assertEqual(len(first["daily"]), 3)
        self.assertEqual(len(first["weekly"]), 1)
        self.assertNotEqual(
            [task["id"] for task in first["daily"]],
            [task["id"] for task in next_day["daily"]],
        )
        self.assertNotEqual(first["weekly"][0]["id"], next_week_quests["weekly"][0]["id"])

    def test_currency_receipt_remains_deliverable_after_season_end(self):
        now = bot.WINTER_START + 20
        task = bot.winter_active_quests(now)["daily"][0]
        self.save_profile(now, lambda profile: profile["daily"].update({task["counter"]: task["goal"]}))
        _, claimed = self.call_api("/api/winter/claim-task", {"taskId": task["id"]}, now + 1)
        receipt_id = claimed["state"]["currencyReceipts"][0]["id"]
        _, ended = self.call_api("/api/winter/state", now=bot.WINTER_END + 1)
        self.assertFalse(ended["active"])
        self.assertEqual(ended["currencyReceipts"][0]["id"], receipt_id)
        _, ack = self.call_api("/api/winter/receipt/ack", {"receiptId": receipt_id}, bot.WINTER_END + 2)
        self.assertTrue(ack["ok"])
        _, ended = self.call_api("/api/winter/state", now=bot.WINTER_END + 3)
        self.assertEqual(ended["currencyReceipts"], [])

    def test_winter_booster_is_consumed_and_uses_server_expiration(self):
        now = bot.WINTER_START + 20
        self.save_profile(now, lambda profile: profile["inventory"].update(boosters=1))
        _, state = self.call_api("/api/winter/booster/use", now=now + 1)
        self.assertEqual(state["inventory"]["boosters"], 0)
        self.assertEqual(state["boosterUntil"], now + 61)
        with self.assertRaises(bot.WinterError) as error:
            self.call_api("/api/winter/booster/use", now=now + 2)
        self.assertEqual(error.exception.code, "no_winter_boosters")

    def test_arena_rejects_replayed_hits_and_finish_rewards_only_once(self):
        started = bot.WINTER_START + 100
        _, run = self.call_api("/api/winter/arena/start", now=started)
        token = run["targetTokens"][0]
        _, hit = self.call_api(
            "/api/winter/arena/hit",
            {"runId": run["runId"], "targetToken": token},
            started + 0.5,
        )
        self.assertEqual(hit["hits"], 1)
        with self.assertRaises(bot.WinterError) as replay:
            self.call_api(
                "/api/winter/arena/hit",
                {"runId": run["runId"], "targetToken": token},
                started + 1,
            )
        self.assertEqual(replay.exception.code, "target_already_hit")
        with self.assertRaises(bot.WinterError) as early:
            self.call_api("/api/winter/arena/finish", {"runId": run["runId"]}, started + 29)
        self.assertEqual(early.exception.code, "arena_too_early")

        _, finished = self.call_api("/api/winter/arena/finish", {"runId": run["runId"]}, started + 30)
        _, repeated = self.call_api("/api/winter/arena/finish", {"runId": run["runId"]}, started + 31)
        self.assertEqual(finished["result"]["score"], 10)
        self.assertTrue(repeated["alreadyFinished"])
        self.assertEqual(repeated["result"], finished["result"])
        _, state = self.call_api("/api/winter/state", now=started + 32)
        self.assertTrue(any(task["progress"] > 0 for task in state["tasks"]["daily"]))
        self.assertEqual(len(state["tasks"]["weekly"]), 1)

    def test_stars_precheckout_checks_amount_owner_and_season(self):
        self.assertLessEqual(len(bot.ITEMS["winter_pass"]["title"]), 32)
        with mock.patch.object(bot, "tg") as telegram:
            with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START - 1):
                bot.handle_update({"pre_checkout_query": {
                    "id": "early", "invoice_payload": "winter_pass:12345:1",
                    "from": {"id": 12345}, "currency": "XTR", "total_amount": 149,
                }})
            self.assertFalse(telegram.call_args.kwargs["ok"])

            with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START):
                bot.handle_update({"pre_checkout_query": {
                    "id": "valid", "invoice_payload": "winter_pass:12345:1",
                    "from": {"id": 12345}, "currency": "XTR", "total_amount": 149,
                }})
            self.assertTrue(telegram.call_args.kwargs["ok"])

            with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START):
                bot.handle_update({"pre_checkout_query": {
                    "id": "wrong-amount", "invoice_payload": "winter_pass:12345:1",
                    "from": {"id": 12345}, "currency": "XTR", "total_amount": 1,
                }})
            self.assertFalse(telegram.call_args.kwargs["ok"])

    def test_successful_stars_update_persists_pass_purchase(self):
        payment_time = bot.WINTER_END + 1
        with mock.patch.object(bot, "tg"), mock.patch.object(bot.time, "time", return_value=payment_time):
            bot.handle_update({"message": {
                "chat": {"id": 12345},
                "from": {"id": 12345},
                "successful_payment": {
                    "invoice_payload": "winter_pass:12345:1",
                    "currency": "XTR",
                    "total_amount": 149,
                    "telegram_payment_charge_id": "charge-1",
                },
            }})
        self.assertEqual(bot.db_counts(12345)["winter_pass"], 1)
        with mock.patch.object(bot.time, "time", return_value=bot.WINTER_START + 1):
            _, state = bot.winter_api("/api/winter/state", {}, self.user)
        self.assertTrue(state["premium"])
        self.assertFalse(state["offerAvailable"])


if __name__ == "__main__":
    unittest.main()
