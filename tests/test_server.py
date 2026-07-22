import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from server import AccountStore, DirectNinebotClient, NinePlusAdapter, Settings


class DirectNinebotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp.name)
        (self.config_dir / "tokens.json").write_text("{}", encoding="utf-8")
        self.settings = Settings("", ninebot_config_dir=self.config_dir)

    def tearDown(self):
        self.temp.cleanup()

    @patch("server.subprocess.run")
    def test_vehicle_discovery_uses_ninecli(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = '[{"wnumber":"SN1","device_name":"Bike"}]'
        run.return_value.stderr = ""
        vehicles = DirectNinebotClient(self.settings).vehicles()
        self.assertEqual(vehicles[0]["wnumber"], "SN1")
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["vehicles", "--json"])

    @patch("server.subprocess.run")
    def test_direct_controls_use_supported_commands(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "{}"
        run.return_value.stderr = ""
        client = DirectNinebotClient(self.settings)
        client.action("SN1", "engine_start")
        command = run.call_args.args[0]
        self.assertEqual(command[-4:], ["engine-start", "SN1", "--yes", "--json"])

    def test_base64_passwords_preserve_special_characters(self):
        values = {
            "NINEBOT_PASSWORD_B64": "cCQjIHdvcmQ=",
            "NINEPLUS_PASSWORD_B64": "YXBwJCNwYXNz",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.ninebot_password, "p$# word")
        self.assertEqual(settings.password, "app$#pass")

    @patch("server.subprocess.run")
    def test_travel_detail_uses_ninecli_detail_and_preserves_track(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({
            "travel_id": "ride-123",
            "trail": [
                {"lon": 121.4, "lat": 31.2, "speed": 12.5, "distance": 0},
                {"lon": 121.401, "lat": 31.201, "speed": 18.0, "distance": 146},
            ],
        })
        run.return_value.stderr = ""
        detail = DirectNinebotClient(self.settings).travel_detail("SN1", "ride-123")
        self.assertEqual(detail["trail"][1]["speed"], 18.0)
        command = run.call_args.args[0]
        self.assertEqual(command[-5:], ["travel", "SN1", "--detail", "ride-123", "--json"])


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            "gateway-token", ninebot_config_dir=root / "ninebot",
            accounts_path=root / "ninebot" / "accounts.json", admin_password="admin-secret",
        )

    def tearDown(self):
        self.temp.cleanup()

    @patch("server.subprocess.run")
    def test_accounts_have_hashed_passwords_and_isolated_tokens(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "[]"
        run.return_value.stderr = ""
        store = AccountStore(self.settings)
        first = store.add_account("13900000001", "alice-pass", "13800000001", "ninebot-a")
        second = store.add_account("13900000002", "bob-password", "13800000002", "ninebot-b")

        payload = json.loads(self.settings.accounts_path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload)
        self.assertNotIn("alice-pass", serialized)
        self.assertNotIn("ninebot-a", serialized)
        self.assertNotEqual(first["config_dir"], second["config_dir"])
        self.assertIsNotNone(store.authenticate("13900000001", "alice-pass"))
        self.assertIsNone(store.authenticate("13900000001", "wrong-pass"))

    @patch("server.subprocess.run")
    def test_new_accounts_require_mobile_numbers_and_eight_character_passwords(self, run):
        store = AccountStore(self.settings)
        invalid_cases = [
            ("alice", "alice-pass", "13800000001", "NineBot+ 账号"),
            ("13900000001", "short7", "13800000001", "至少需要 8 位"),
            ("13900000001", "alice-pass", "ninebot", "九号出行账号"),
        ]
        for app_account, app_password, ninebot_username, message in invalid_cases:
            with self.subTest(message=message), self.assertRaises(ValueError) as error:
                store.add_account(app_account, app_password, ninebot_username, "ninebot-a")
            self.assertIn(message, str(error.exception))
        run.assert_not_called()

    def test_admin_password_is_initialized_on_first_use_and_persisted(self):
        settings = self.settings
        settings = Settings(
            settings.bearer_token, settings.account, settings.password,
            ninebot_config_dir=settings.ninebot_config_dir, accounts_path=settings.accounts_path,
            admin_password="",
        )
        store = AccountStore(settings)
        self.assertFalse(store.admin_configured())
        store.setup_admin("admin-pass")
        self.assertTrue(store.admin_configured())
        self.assertTrue(store.authenticate_admin("admin-pass"))
        self.assertFalse(store.authenticate_admin("wrong-pass"))
        with self.assertRaisesRegex(ValueError, "当前管理员密码错误"):
            store.change_admin_password("wrong-pass", "new-admin-pass")
        with self.assertRaisesRegex(ValueError, "至少需要 8 位"):
            store.change_admin_password("admin-pass", "short")
        store.change_admin_password("admin-pass", "new-admin-pass")
        reloaded = AccountStore(settings)
        self.assertFalse(reloaded.authenticate_admin("admin-pass"))
        self.assertTrue(reloaded.authenticate_admin("new-admin-pass"))

    def test_persisted_admin_password_overrides_legacy_environment_password(self):
        store = AccountStore(self.settings)
        self.assertTrue(store.authenticate_admin("admin-secret"))
        store.change_admin_password("admin-secret", "changed-admin")
        reloaded = AccountStore(self.settings)
        self.assertFalse(reloaded.authenticate_admin("admin-secret"))
        self.assertTrue(reloaded.authenticate_admin("changed-admin"))

    @patch("server.subprocess.run")
    def test_adapter_session_resolves_only_authenticated_account(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "[]"
        run.return_value.stderr = ""
        store = AccountStore(self.settings)
        alice = store.add_account("13900000001", "alice-pass", "13800000001", "ninebot-a")
        bob = store.add_account("13900000002", "bob-password", "13800000002", "ninebot-b")
        (Path(alice["config_dir"]) / "tokens.json").write_text("{}", encoding="utf-8")
        (Path(bob["config_dir"]) / "tokens.json").write_text("{}", encoding="utf-8")
        adapter = NinePlusAdapter(self.settings)

        self.assertIsNone(adapter.login("13900000001", "wrong-pass"))
        alice_login = adapter.login("13900000001", "alice-pass")
        bob_login = adapter.login("13900000002", "bob-password")
        self.assertIsNotNone(alice_login)
        self.assertIsNotNone(bob_login)
        alice_client = adapter.client_for_session(alice_login["session_token"])
        bob_client = adapter.client_for_session(bob_login["session_token"])
        self.assertIsNotNone(alice_client)
        self.assertIsNotNone(bob_client)
        self.assertNotEqual(alice_client.settings.ninebot_config_dir, bob_client.settings.ninebot_config_dir)
        self.assertIsNone(adapter.client_for_session("invalid-session"))


if __name__ == "__main__":
    unittest.main()
