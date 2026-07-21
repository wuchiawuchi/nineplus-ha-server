import json
import tempfile
import unittest
from pathlib import Path

from server import NinePlusAdapter, Settings


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


if __name__ == "__main__":
    unittest.main()
