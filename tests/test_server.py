import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from server import AccountStore, DirectNinebotClient, NinePlusAdapter, Settings


class FakeHA:
    states = {
        "sensor.battery": {"state": "79", "attributes": {}},
        "sensor.range": {"state": "62.5", "attributes": {}},
        "binary_sensor.charging": {"state": "off", "attributes": {}},
        "device_tracker.bike": {"state": "home", "attributes": {"latitude": 31.2, "longitude": 121.4}},
    }

    def state(self, entity_id):
        return self.states[entity_id]

    def check(self):
        return None

    def call_service(self, domain, service, data):
        return {"domain": domain, "service": service, "data": data}


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        config = {
            "vehicles": [{
                "sn": "SN123",
                "name": "Test Bike",
                "model": "MMAX",
                "integration": "custom",
                "entities": {
                    "battery": "sensor.battery",
                    "range": "sensor.range",
                    "charging": "binary_sensor.charging",
                    "latitude": {"entity_id": "device_tracker.bike", "attribute": "latitude"},
                    "longitude": {"entity_id": "device_tracker.bike", "attribute": "longitude"}
                },
                "services": {"bell": {"service": "ninebot.ring_bell", "data": {"vehicle": "SN123"}}}
            }]
        }
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        settings = Settings("http://ha:8123", "token", "", "", "", path)
        self.adapter = NinePlusAdapter(settings, FakeHA())

    def tearDown(self):
        self.temp.cleanup()

    def test_dashboard_maps_entities(self):
        dashboard = self.adapter.dashboard("SN123")
        self.assertEqual(dashboard["state"]["dump_energy"], 79)
        self.assertEqual(dashboard["state"]["estimate_mileage"], 62.5)
        self.assertEqual(dashboard["state"]["charging"], 0)
        self.assertEqual(dashboard["state"]["locationInfo"]["lat"], 31.2)

    def test_action_calls_ha_service(self):
        result = self.adapter.action("SN123", "bell")
        self.assertEqual(result["domain"], "ninebot")
        self.assertEqual(result["service"], "ring_bell")

    def test_unknown_vehicle(self):
        with self.assertRaises(KeyError):
            self.adapter.dashboard("missing")


class HassccNinebotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "config.json"
        path.write_text(json.dumps({"vehicles": [{"sn": "SNABC", "name": "My Ninebot"}]}), encoding="utf-8")
        settings = Settings("http://ha:8123", "token", "", "", "", path)
        fake = FakeHA()
        fake.states = {
            "ninebot.snabc_battery": {"state": "88", "attributes": {}},
            "ninebot.snabc_endurance": {"state": "47.2", "attributes": {}},
            "ninebot.snabc_charging": {"state": "off", "attributes": {}},
            "ninebot.snabc_power": {"state": "on", "attributes": {}},
            "ninebot.snabc_lock": {"state": "locked", "attributes": {}},
            "ninebot.snabc_location": {"state": "home", "attributes": {"latitude": 31.2, "longitude": 121.4}},
            "ninebot.snabc_month_mileage": {"state": "123.4", "attributes": {}},
            "ninebot.snabc_last_mileage": {"state": "3.2", "attributes": {"start_time": "2026-07-20"}},
            "ninebot.snabc_month_energy": {"state": "456", "attributes": {}},
            "ninebot.snabc_bms_voltage": {"state": "55.8", "attributes": {}},
            "ninebot.snabc_batt_temp": {"state": "29.5", "attributes": {}},
            "ninebot.snabc_bms_cycles": {"state": "42", "attributes": {"bms_score": 96}},
            "ninebot.snabc_remaining_charge_time": {"state": "unknown", "attributes": {}},
        }
        self.adapter = NinePlusAdapter(settings, fake)

    def tearDown(self):
        self.temp.cleanup()

    def test_hasscc_entities_are_automatic(self):
        dashboard = self.adapter.dashboard("SNABC")
        self.assertEqual(dashboard["state"]["dump_energy"], 88)
        self.assertEqual(dashboard["state"]["precise_estimate_mileage"], 47.2)
        self.assertEqual(dashboard["battery"]["battery_voltage"], 55.8)
        self.assertEqual(dashboard["travel"]["total_mileages"], 123.4)

    def test_hasscc_standard_entity_services(self):
        result = self.adapter.action("SNABC", "bell")
        self.assertEqual((result["domain"], result["service"]), ("button", "press"))
        self.assertEqual(result["data"]["entity_id"], "ninebot.snabc_bell")
        result = self.adapter.action("SNABC", "engine_start")
        self.assertEqual((result["domain"], result["service"]), ("lock", "unlock"))


class DirectNinebotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp.name)
        (self.config_dir / "tokens.json").write_text("{}", encoding="utf-8")
        self.settings = Settings("", "", "", "", "", Path("unused"), backend="direct", ninebot_config_dir=self.config_dir)

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
            "", "", "gateway-token", "", "", root / "config.json",
            backend="direct", ninebot_config_dir=root / "ninebot",
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
            settings.ha_url, settings.ha_token, settings.bearer_token, settings.account,
            settings.password, settings.config_path, backend="direct",
            ninebot_config_dir=settings.ninebot_config_dir, accounts_path=settings.accounts_path,
            admin_password="",
        )
        store = AccountStore(settings)
        self.assertFalse(store.admin_configured())
        store.setup_admin("admin-pass")
        self.assertTrue(store.admin_configured())
        self.assertTrue(store.authenticate_admin("admin-pass"))
        self.assertFalse(store.authenticate_admin("wrong-pass"))

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
