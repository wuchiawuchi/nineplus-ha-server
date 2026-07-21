import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from server import DirectNinebotClient, NinePlusAdapter, Settings


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


if __name__ == "__main__":
    unittest.main()
