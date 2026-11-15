import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "test-token")
import bot


class StarterPackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_players_file = bot.PLAYERS_FILE
        self.old_db_file = bot.DB_FILE
        bot.PLAYERS_FILE = os.path.join(self.temp_dir.name, "players.json")
        bot.DB_FILE = os.path.join(self.temp_dir.name, "purchases.json")

    def tearDown(self):
        bot.PLAYERS_FILE = self.old_players_file
        bot.DB_FILE = self.old_db_file
        self.temp_dir.cleanup()

    def test_only_new_players_get_the_offer_and_it_can_be_dismissed(self):
        with open(bot.PLAYERS_FILE, "w", encoding="utf-8") as file:
            json.dump({"202": 100}, file)

        new_player = bot.player_count_open(101)
        self.assertEqual(new_player["count"], 2)
        self.assertTrue(new_player["starterPackEligible"])
        self.assertTrue(new_player["starterPackOfferAvailable"])

        self.assertTrue(bot.starter_pack_mark_seen(101))
        after_dismiss = bot.player_count_open(101)
        self.assertTrue(after_dismiss["starterPackEligible"])
        self.assertFalse(after_dismiss["starterPackOfferAvailable"])
        self.assertFalse(bot.starter_pack_can_buy(202))
        self.assertTrue(bot.starter_pack_can_buy(101))

    def test_pack_is_one_time_and_payment_record_disables_another_invoice(self):
        bot.player_count_open(101)
        self.assertTrue(bot.starter_pack_can_buy(101))
        bot.db_add(101, "starter_pack", "charge-1")
        self.assertFalse(bot.starter_pack_can_buy(101))
        self.assertFalse(bot.player_count_open(101)["starterPackEligible"])
        self.assertEqual(bot.db_counts(101)["starter_pack"], 1)

    def test_corrupt_purchase_storage_fails_closed(self):
        bot.player_count_open(101)
        with open(bot.DB_FILE, "w", encoding="utf-8") as file:
            file.write("{")
        with self.assertRaises(RuntimeError):
            bot.starter_pack_can_buy(101)

    def test_new_player_can_create_invoice_but_existing_player_cannot(self):
        with open(bot.PLAYERS_FILE, "w", encoding="utf-8") as file:
            json.dump({"202": 100}, file)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"

        def post(path, user_id, payload):
            request = urllib.request.Request(
                endpoint + path,
                data=json.dumps({"initData": "signed", **payload}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with mock.patch.object(bot, "validate_init_data", return_value={"id": user_id}):
                with urllib.request.urlopen(request) as response:
                    return json.loads(response.read())

        try:
            with mock.patch.object(bot, "tg", return_value={"ok": True, "result": "https://invoice"}):
                opened = post("/api/players/open", 101, {})
                invoice = post("/api/invoice", 101, {"item": "starter_pack"})
            self.assertTrue(opened["starterPackOfferAvailable"])
            self.assertEqual(invoice["link"], "https://invoice")
            with self.assertRaises(urllib.error.HTTPError) as error:
                post("/api/invoice", 202, {"item": "starter_pack"})
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_successful_stars_payment_is_recorded_for_purchase_sync(self):
        bot.player_count_open(101)
        update = {
            "message": {
                "chat": {"id": 101},
                "from": {"id": 101},
                "successful_payment": {
                    "invoice_payload": "starter_pack:101:123",
                    "currency": "XTR",
                    "total_amount": 99,
                    "telegram_payment_charge_id": "charge-1",
                },
            }
        }
        with mock.patch.object(bot, "tg", return_value={"ok": True}):
            bot.handle_update(update)
        self.assertEqual(bot.db_counts(101), {"starter_pack": 1})

    def test_precheckout_rejects_non_new_or_already_purchased_users(self):
        bot.player_count_open(101)
        bot.player_count_open(202)
        bot.db_add(202, "starter_pack", "charge-2")

        def precheckout(user_id):
            return {
                "pre_checkout_query": {
                    "id": "checkout",
                    "invoice_payload": f"starter_pack:{user_id}:123",
                    "from": {"id": user_id},
                    "currency": "XTR",
                    "total_amount": 99,
                }
            }

        with mock.patch.object(bot, "tg") as telegram:
            bot.handle_update(precheckout(101))
            self.assertTrue(telegram.call_args.kwargs["ok"])
            bot.handle_update(precheckout(202))
            self.assertFalse(telegram.call_args.kwargs["ok"])


if __name__ == "__main__":
    unittest.main()
