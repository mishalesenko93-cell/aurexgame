import os
import http.client
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")
import bot


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_referrals_file = bot.REFERRALS_FILE
        bot.REFERRALS_FILE = os.path.join(self.temp_dir.name, "referrals.json")

    def tearDown(self):
        bot.REFERRALS_FILE = self.old_referrals_file
        self.temp_dir.cleanup()

    def test_referral_activates_once_and_issues_one_receipt(self):
        self.assertTrue(bot.referral_register(100, 200, now=123))
        self.assertFalse(bot.referral_register(100, 200, now=124))
        self.assertFalse(bot.referral_register(200, 200, now=125))
        self.assertFalse(bot.referral_activate(200, bot.REFERRAL_TAP_GOAL - 1))
        self.assertTrue(bot.referral_activate(200, bot.REFERRAL_TAP_GOAL))
        self.assertFalse(bot.referral_activate(200, bot.REFERRAL_TAP_GOAL + 1))

        with mock.patch.object(bot, "bot_username", return_value="AurexBot"):
            state = bot.referral_state({"id": 100})
        self.assertEqual(state["inviteLink"], "https://t.me/AurexBot?start=ref_100")
        self.assertEqual(state["invited"], 1)
        self.assertEqual(state["activated"], 1)
        self.assertEqual(len(state["receipts"]), 1)
        self.assertEqual(state["receipts"][0]["amount"], bot.REFERRAL_REWARD_COINS)
        self.assertTrue(bot.referral_ack(100, state["receipts"][0]["id"]))
        with mock.patch.object(bot, "bot_username", return_value="AurexBot"):
            self.assertEqual(bot.referral_state({"id": 100})["receipts"], [])


class PlayerCounterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_players_file = bot.PLAYERS_FILE
        bot.PLAYERS_FILE = os.path.join(self.temp_dir.name, "players.json")

    def tearDown(self):
        bot.PLAYERS_FILE = self.old_players_file
        self.temp_dir.cleanup()

    def test_counts_unique_telegram_users_only(self):
        with mock.patch.object(bot.time, "time", return_value=100):
            self.assertEqual(bot.player_count_record(123), 1)
            self.assertEqual(bot.player_count_record(123), 1)
            self.assertEqual(bot.player_count_record(456), 2)
        with self.assertRaises(ValueError):
            bot.player_count_record("not-a-telegram-id")

    def test_open_endpoint_counts_authenticated_id_not_client_supplied_id(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/api/players/open"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps({"initData": "signed", "userId": 999}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with mock.patch.object(bot, "validate_init_data", return_value={"id": 123}):
                with urllib.request.urlopen(request) as response:
                    first = json.loads(response.read())
                with urllib.request.urlopen(request) as response:
                    second = json.loads(response.read())
            self.assertEqual(first["count"], 1)
            self.assertEqual(second["count"], 1)
            with open(bot.PLAYERS_FILE, "r", encoding="utf-8") as file:
                self.assertEqual(list(json.load(file)), ["123"])

            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            with mock.patch.object(bot, "validate_init_data", return_value=None):
                connection.request(
                    "POST",
                    "/api/players/open",
                    body=json.dumps({"initData": "invalid"}),
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
            self.assertEqual(response.status, 401)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
