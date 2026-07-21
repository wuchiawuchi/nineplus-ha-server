#!/usr/bin/env python3
"""NinePlus-compatible HTTP adapter backed by Home Assistant entities."""

from __future__ import annotations

import json
import base64
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret_env(name: str, legacy_name: str) -> str:
    encoded = _env(name)
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"{name} 不是有效的 Base64 配置") from exc
    return _env(legacy_name)


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(str(value).strip())
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "charging", "locked", "powered_on"}:
        return True
    if normalized in {"0", "false", "no", "off", "idle", "unlocked", "powered_off"}:
        return False
    return None


@dataclass(frozen=True)
class Settings:
    ha_url: str
    ha_token: str
    bearer_token: str
    account: str
    password: str
    config_path: Path
    backend: str = "home_assistant"
    ninebot_username: str = ""
    ninebot_password: str = ""
    ninebot_config_dir: Path = Path("/data/ninebot")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            ha_url=_env("HA_URL", "http://homeassistant.local:8123").rstrip("/"),
            ha_token=_env("HA_TOKEN"),
            bearer_token=_env("NINEPLUS_BEARER_TOKEN"),
            account=_env("NINEPLUS_ACCOUNT"),
            password=_secret_env("NINEPLUS_PASSWORD_B64", "NINEPLUS_PASSWORD"),
            config_path=Path(_env("NINEPLUS_CONFIG", "/data/config.json")),
            backend=_env("NINEPLUS_BACKEND", "direct").lower(),
            ninebot_username=_env("NINEBOT_USERNAME"),
            ninebot_password=_secret_env("NINEBOT_PASSWORD_B64", "NINEBOT_PASSWORD"),
            ninebot_config_dir=Path(_env("NINEBOT_CONFIG_DIR", "/data/ninebot")),
        )


class DirectNinebotClient:
    """Small synchronous wrapper around the same ninecli used by hasscc/ninebot."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ninebot_config_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not (self.settings.ninebot_config_dir / "tokens.json").exists():
            if not settings.ninebot_username or not settings.ninebot_password:
                raise RuntimeError("首次启动需要 NINEBOT_USERNAME 和 NINEBOT_PASSWORD")
            self.run("login", "-u", settings.ninebot_username, "-p", settings.ninebot_password)

    def run(self, *args: str) -> Any:
        command = [
            sys.executable, "-m", "ninecli", "--config",
            str(self.settings.ninebot_config_dir), *args, "--json",
        ]
        with self._lock:
            result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "ninecli 调用失败").strip()
            raise RuntimeError(detail[:500])
        try:
            return json.loads(result.stdout or "{}")
        except ValueError as exc:
            raise RuntimeError("ninecli 返回了无效 JSON") from exc

    def vehicles(self) -> list[dict[str, Any]]:
        payload = self.run("vehicles")
        values = payload if isinstance(payload, list) else payload.get("data", [])
        return values if isinstance(values, list) else []

    def dashboard(self, sn: str) -> dict[str, Any]:
        month = datetime.now().strftime("%Y%m")
        status = self.run("status", sn)
        battery = self.run("battery", sn)
        try:
            travel = self.run("travel", sn, "--month", month)
        except RuntimeError:
            travel = {"month": month, "list": []}
        return {
            "vehicle": next((v for v in self.vehicles() if str(v.get("wnumber") or v.get("sn")) == sn), {"sn": sn, "wnumber": sn}),
            "status": status,
            "battery": battery,
            "travel": travel,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def action(self, sn: str, action: str) -> Any:
        commands = {
            "bell": ("bell", sn),
            "buck": ("buck", sn, "--yes"),
            "engine_start": ("engine-start", sn, "--yes"),
            "engine_stop": ("engine-stop", sn, "--yes"),
        }
        command = commands.get(action)
        if command is None:
            raise NotImplementedError(action)
        return self.run(*command)


class HomeAssistantClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.settings.ha_url}/api/{path.lstrip('/')}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.settings.ha_token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Home Assistant HTTP {exc.code}: {detail[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 Home Assistant: {exc.reason}") from exc

    def check(self) -> None:
        self._request("GET", "")

    def state(self, entity_id: str) -> dict[str, Any]:
        return self._request("GET", f"states/{urllib.parse.quote(entity_id, safe='.')}")

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return self._request("POST", f"services/{domain}/{service}", data)


class NinePlusAdapter:
    def __init__(self, settings: Settings, ha: HomeAssistantClient | None = None):
        self.settings = settings
        self.direct = DirectNinebotClient(settings) if settings.backend == "direct" and ha is None else None
        self.ha = ha or (None if self.direct else HomeAssistantClient(settings))
        self.config = {"vehicles": []} if self.direct else self._load_config()
        self.session_token = secrets.token_urlsafe(24)

    def _load_config(self) -> dict[str, Any]:
        if not self.settings.config_path.exists():
            raise RuntimeError(f"配置文件不存在: {self.settings.config_path}")
        with self.settings.config_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        vehicles = config.get("vehicles")
        if not isinstance(vehicles, list) or not vehicles:
            raise RuntimeError("config.json 至少需要一辆 vehicles 配置")
        for vehicle in vehicles:
            if not vehicle.get("sn"):
                raise RuntimeError("每辆车都必须配置 sn")
        return config

    def vehicle(self, sn: str) -> dict[str, Any]:
        if self.direct:
            for vehicle in self.direct.vehicles():
                if str(vehicle.get("wnumber") or vehicle.get("sn")) == sn:
                    return vehicle
            raise KeyError(sn)
        for vehicle in self.config["vehicles"]:
            if str(vehicle["sn"]) == sn:
                return vehicle
        raise KeyError(sn)

    @staticmethod
    def _hasscc_entity(sn: str, key: str) -> str:
        """Entity id emitted by hasscc/ninebot's NinebotEntity class."""
        return f"ninebot.{sn}_{key}".lower()

    def _entity_spec(self, vehicle: dict[str, Any], key: str) -> Any:
        explicit = vehicle.get("entities", {}).get(key)
        if explicit is not None:
            return explicit
        if vehicle.get("integration", "hasscc/ninebot") == "hasscc/ninebot":
            aliases = {
                "range": "endurance",
                "powered_on": "power",
                "locked": "lock",
                "latitude": "location",
                "longitude": "location",
                "voltage": "bms_voltage",
                "temperature": "batt_temp",
                "total_mileage": "month_mileage",
            }
            entity_key = aliases.get(key, key)
            spec: dict[str, Any] = {"entity_id": self._hasscc_entity(str(vehicle["sn"]), entity_key)}
            if key in {"latitude", "longitude"}:
                spec["attribute"] = key
            return spec
        return None

    @staticmethod
    def vehicle_info(vehicle: dict[str, Any]) -> dict[str, Any]:
        return {
            "sn": str(vehicle["sn"]),
            "wnumber": str(vehicle["sn"]),
            "device_name": vehicle.get("name", "Ninebot"),
            "vehicle_name": vehicle.get("model", "Ninebot"),
            "v6_light_img_url": vehicle.get("image_url"),
        }

    def _entity_value(self, spec: Any, default: Any = None) -> Any:
        if spec is None:
            return default
        if not isinstance(spec, dict):
            spec = {"entity_id": spec}
        entity_id = spec.get("entity_id")
        if not entity_id:
            return spec.get("value", default)
        try:
            state = self.ha.state(str(entity_id))
        except (KeyError, RuntimeError):
            return default
        attribute = spec.get("attribute")
        value = state.get("attributes", {}).get(attribute) if attribute else state.get("state")
        mapping = spec.get("map", {})
        value = mapping.get(str(value), value)
        scale = _number(spec.get("scale", 1)) or 1
        numeric = _number(value)
        if numeric is not None and scale != 1:
            value = numeric * scale
        return value if value is not None else default

    def dashboard(self, sn: str) -> dict[str, Any]:
        if self.direct:
            return self.direct.dashboard(sn)
        vehicle = self.vehicle(sn)
        value = lambda key, default=None: self._entity_value(self._entity_spec(vehicle, key), default)
        battery = _number(value("battery"))
        charging = _bool(value("charging"))
        powered = _bool(value("powered_on"))
        locked = _bool(value("locked"))
        try:
            last_ride_state = self.ha.state(self._hasscc_entity(sn, "last_mileage")) if vehicle.get("integration", "hasscc/ninebot") == "hasscc/ninebot" else None
        except (KeyError, RuntimeError):
            last_ride_state = None
        last_ride = last_ride_state.get("attributes", {}) if isinstance(last_ride_state, dict) else {}
        state = {
            "dump_energy": battery,
            "estimate_mileage": _number(value("range")),
            "precise_estimate_mileage": _number(value("range")),
            "charging": int(charging) if charging is not None else None,
            "pwr": int(powered) if powered is not None else None,
            "lock_status": int(locked) if locked is not None else None,
            "total_mileage": _number(value("total_mileage")),
            "locationInfo": {
                "locationDesc": value("location"),
                "lat": _number(value("latitude")),
                "lng": _number(value("longitude")),
            },
        }
        battery_info = {
            "electricity": battery,
            "battery_voltage": _number(value("voltage")),
            "bat_temp": _number(value("temperature")),
            "bms_cycle": _number(value("bms_cycles")),
            "charging": int(charging) if charging is not None else None,
            "remain_charge_time": _number(value("remaining_charge_time")),
        }
        rides = [last_ride] if last_ride else []
        return {
            "vehicle": self.vehicle_info(vehicle),
            "state": {key: value for key, value in state.items() if value is not None},
            "battery": {key: value for key, value in battery_info.items() if value is not None},
            "travel": {
                "month": datetime.now().strftime("%Y%m"),
                "list": rides,
                "total_mileages": _number(value("total_mileage")),
                "ec": _number(value("month_energy")),
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def action(self, sn: str, action: str) -> Any:
        if self.direct:
            return self.direct.action(sn, action)
        vehicle = self.vehicle(sn)
        spec = vehicle.get("services", {}).get(action)
        if not spec and vehicle.get("integration", "hasscc/ninebot") == "hasscc/ninebot":
            defaults = {
                "bell": {"service": "button.press", "data": {"entity_id": self._hasscc_entity(sn, "bell")}},
                "buck": {"service": "button.press", "data": {"entity_id": self._hasscc_entity(sn, "bucket")}},
                "engine_start": {"service": "lock.unlock", "data": {"entity_id": self._hasscc_entity(sn, "lock")}},
                "engine_stop": {"service": "lock.lock", "data": {"entity_id": self._hasscc_entity(sn, "lock")}},
            }
            spec = defaults.get(action)
        if not spec:
            raise NotImplementedError(f"Home Assistant 未配置 {action} 服务")
        domain, separator, service = str(spec.get("service", "")).partition(".")
        if not separator or not domain or not service:
            raise RuntimeError(f"{action} 的 service 必须使用 domain.service 格式")
        return self.ha.call_service(domain, service, dict(spec.get("data", {})))


class Handler(BaseHTTPRequestHandler):
    adapter: NinePlusAdapter
    server_version = "NinePlusHA/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def _reply(self, status: int, data: Any = None, error: str | None = None) -> None:
        payload = {"ok": error is None}
        payload["data" if error is None else "error"] = data if error is None else {"message": error}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        required = self.adapter.settings.bearer_token
        return not required or self.headers.get("Authorization") == f"Bearer {required}"

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        method = self.command
        body = self._json_body() if method == "POST" else {}

        if parts == ["healthz"] and method == "GET":
            if self.adapter.direct:
                self.adapter.direct.vehicles()
                backend = "ninebot-cloud"
            else:
                self.adapter.ha.check()
                backend = "home-assistant"
            self._reply(HTTPStatus.OK, {"status": "ok", "backend": backend})
            return
        if not self._authorized():
            self._reply(HTTPStatus.UNAUTHORIZED, error="Bearer Token 无效")
            return
        if parts == ["accounts", "login"] and method == "POST":
            configured_account = self.adapter.settings.account
            configured_password = self.adapter.settings.password
            if configured_account and body.get("account") != configured_account:
                self._reply(HTTPStatus.UNAUTHORIZED, error="账号错误")
                return
            if configured_password and body.get("password") != configured_password:
                self._reply(HTTPStatus.UNAUTHORIZED, error="密码错误")
                return
            self._reply(HTTPStatus.OK, {
                "phone": body.get("account", "home-assistant"),
                "session_token": self.adapter.session_token,
            })
            return
        if parts == ["vehicles"] and method == "GET":
            vehicles = self.adapter.direct.vehicles() if self.adapter.direct else [self.adapter.vehicle_info(v) for v in self.adapter.config["vehicles"]]
            self._reply(HTTPStatus.OK, {"vehicles": vehicles})
            return
        if len(parts) >= 3 and parts[0] == "vehicles":
            sn, endpoint = parts[1], parts[2]
            if method == "GET" and endpoint in {"dashboard", "status", "battery"}:
                dashboard = self.adapter.dashboard(sn)
                value = dashboard if endpoint == "dashboard" else dashboard["state" if endpoint == "status" else "battery"]
                self._reply(HTTPStatus.OK, value)
                return
            if method == "GET" and endpoint == "travel":
                month = urllib.parse.parse_qs(parsed.query).get("month", [datetime.now().strftime("%Y%m")])[0]
                self._reply(HTTPStatus.OK, {"month": month, "list": [], "total": 0})
                return
            if method == "POST" and endpoint == "travel-sync":
                month = urllib.parse.parse_qs(parsed.query).get("month", [datetime.now().strftime("%Y%m")])[0]
                self._reply(HTTPStatus.OK, {"month": month, "records": [], "total": 0})
                return
            if method == "GET" and endpoint == "prediction":
                self._reply(HTTPStatus.OK, {})
                return
            if method == "POST" and endpoint == "prediction-settings":
                self._reply(HTTPStatus.OK, {"battery_chemistry": body})
                return
            action = None
            if method == "POST" and endpoint in {"bell", "buck"}:
                action = endpoint
            elif method == "POST" and len(parts) == 4 and endpoint == "engine" and parts[3] in {"start", "stop"}:
                action = f"engine_{parts[3]}"
            if action:
                self._reply(HTTPStatus.OK, self.adapter.action(sn, action))
                return
        if method == "POST" and tuple(parts) in {("devices", "register"), ("live-activities", "register")}:
            self._reply(HTTPStatus.OK, {"accepted": False, "reason": "HA adapter does not provide APNs"})
            return
        self._reply(HTTPStatus.NOT_FOUND, error="接口不存在")

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        try:
            self._dispatch()
        except KeyError:
            self._reply(HTTPStatus.NOT_FOUND, error="车辆不存在")
        except NotImplementedError as exc:
            self._reply(HTTPStatus.NOT_IMPLEMENTED, error=str(exc))
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_GATEWAY, error=str(exc))
        except Exception as exc:  # Keep an API response while logging unexpected failures.
            print(f"unexpected error: {exc!r}", file=sys.stderr)
            self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, error="服务端内部错误")


def main() -> None:
    settings = Settings.from_env()
    if settings.backend != "direct" and not settings.ha_token:
        raise SystemExit("Home Assistant 模式下 HA_TOKEN 不能为空")
    Handler.adapter = NinePlusAdapter(settings)
    host = _env("HOST", "0.0.0.0")
    port = int(_env("PORT", "19009"))
    print(f"NinePlus HA adapter listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
