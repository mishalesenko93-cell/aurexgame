import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")
import bot


class LeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_scores_file = bot.SCORES_FILE
        bot.SCORES_FILE = os.path.join(self.temp_dir.name, "leaderboard.json")

    def tearDown(self):
        bot.SCORES_FILE = self.old_scores_file
        self.temp_dir.cleanup()

    def test_only_tappers_are_ranked_and_legacy_players_are_preserved(self):
        with open(bot.SCORES_FILE, "w", encoding="utf-8") as file:
            json.dump({
                "101": {"name": "Legacy", "score": 500},
                "202": {"name": "Never tapped", "score": 0},
            }, file)

        result = bot.score_upsert({"id": 303, "first_name": "New"}, 0, taps=0)
        self.assertEqual(result["count"], 1)
        self.assertEqual([player["name"] for player in result["players"]], ["Legacy"])
        self.assertEqual(result["rank"], 0)

        result = bot.score_upsert({"id": 202, "first_name": "Tapper"}, 200, taps=1)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["rank"], 2)
        self.assertEqual([player["name"] for player in result["players"]], ["Legacy", "Tapper"])

        repeated = bot.score_upsert({"id": 202, "first_name": "Tapper"}, 100, taps=1)
        self.assertEqual(repeated["count"], 2)
        self.assertEqual(repeated["players"][1]["score"], 200)

        with open(bot.SCORES_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)
        self.assertTrue(saved["101"]["tapped"])
        self.assertFalse(saved["303"]["tapped"])

    def test_corrupt_storage_is_not_replaced_with_an_empty_leaderboard(self):
        with open(bot.SCORES_FILE, "w", encoding="utf-8") as file:
            file.write("{")

        with self.assertRaises(RuntimeError):
            bot.score_upsert({"id": 101}, 100, taps=1)
        with open(bot.SCORES_FILE, "r", encoding="utf-8") as file:
            self.assertEqual(file.read(), "{")

    def test_tap_count_must_be_a_nonnegative_integer(self):
        for invalid_taps in (-1, 1.5, True, "1"):
            with self.subTest(taps=invalid_taps), self.assertRaises(ValueError):
                bot.score_upsert({"id": 101}, 0, taps=invalid_taps)

    def test_score_api_adds_a_player_after_the_first_tap(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}/api/score"

        def post_score(taps):
            request = urllib.request.Request(
                endpoint,
                data=json.dumps({"initData": "signed", "score": 0, "taps": taps}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read())

        try:
            with mock.patch.object(bot, "validate_init_data", return_value={"id": 404}):
                unopened = post_score(0)
                tapped = post_score(1)
            self.assertEqual(unopened["count"], 0)
            self.assertEqual(tapped["count"], 1)
            self.assertEqual(tapped["rank"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
