#!/usr/bin/env python3
"""NinePlus-compatible HTTP adapter backed by Home Assistant entities."""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import html
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
    accounts_path: Path = Path("/data/ninebot/accounts.json")
    admin_password: str = ""

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
            accounts_path=Path(_env("NINEPLUS_ACCOUNTS", "/data/ninebot/accounts.json")),
            admin_password=_secret_env("NINEPLUS_ADMIN_PASSWORD_B64", "NINEPLUS_ADMIN_PASSWORD") or _secret_env("NINEPLUS_PASSWORD_B64", "NINEPLUS_PASSWORD"),
        )


class AccountStore:
    """Persistent NineBot+ users with isolated ninecli token directories."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.accounts_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._accounts = self._load()
        self._migrate_legacy_account()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        values = payload.get("accounts", payload) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            raise RuntimeError("accounts.json 格式无效")
        return {str(key): value for key, value in values.items() if isinstance(value, dict)}

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "accounts": self._accounts}, handle, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @staticmethod
    def _password_hash(password: str, salt_hex: str | None = None) -> tuple[str, str]:
        salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
        return salt.hex(), digest.hex()

    @staticmethod
    def _account_id(account: str) -> str:
        return hashlib.sha256(account.casefold().encode("utf-8")).hexdigest()[:24]

    def _migrate_legacy_account(self) -> None:
        if self._accounts or not self.settings.account or not self.settings.password:
            return
        salt, digest = self._password_hash(self.settings.password)
        self._accounts[self.settings.account] = {
            "account_id": self._account_id(self.settings.account),
            "password_salt": salt,
            "password_hash": digest,
            "ninebot_username": self.settings.ninebot_username,
            "config_dir": str(self.settings.ninebot_config_dir),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "migrated": True,
        }
        self._save()

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "account": account,
                    "ninebot_username": record.get("ninebot_username", ""),
                    "created_at": record.get("created_at", ""),
                }
                for account, record in sorted(self._accounts.items())
            ]

    def authenticate(self, account: str, password: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._accounts.get(account)
            if not record:
                return None
            _, digest = self._password_hash(password, str(record.get("password_salt", "")))
            return dict(record) if hmac.compare_digest(digest, str(record.get("password_hash", ""))) else None

    def add_account(self, app_account: str, app_password: str, ninebot_username: str, ninebot_password: str) -> dict[str, Any]:
        app_account = app_account.strip()
        ninebot_username = ninebot_username.strip()
        if not app_account or not app_password or not ninebot_username or not ninebot_password:
            raise ValueError("NineBot+ 账号、NineBot+ 密码、九号账号和九号密码均不能为空")
        if len(app_password) < 6:
            raise ValueError("NineBot+ 密码至少需要 6 位")
        with self._lock:
            if app_account in self._accounts:
                raise ValueError("NineBot+ 账号已存在")
            account_id = self._account_id(app_account)
            config_dir = self.settings.ninebot_config_dir / "accounts" / account_id
            account_settings = self._settings_for(config_dir, ninebot_username, ninebot_password)
            client = DirectNinebotClient(account_settings)
            vehicles = client.vehicles()  # Validate the Ninebot login before persisting the user.
            salt, digest = self._password_hash(app_password)
            record = {
                "account_id": account_id,
                "password_salt": salt,
                "password_hash": digest,
                "ninebot_username": ninebot_username,
                "config_dir": str(config_dir),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._accounts[app_account] = record
            self._save()
            return {"account": app_account, "vehicle_count": len(vehicles), **record}

    def client_settings(self, record: dict[str, Any]) -> Settings:
        return self._settings_for(Path(str(record["config_dir"])), str(record.get("ninebot_username", "")), "")

    def _settings_for(self, config_dir: Path, username: str, password: str) -> Settings:
        return Settings(
            self.settings.ha_url, self.settings.ha_token, self.settings.bearer_token,
            self.settings.account, self.settings.password, self.settings.config_path,
            backend="direct", ninebot_username=username, ninebot_password=password,
            ninebot_config_dir=config_dir, accounts_path=self.settings.accounts_path,
            admin_password=self.settings.admin_password,
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

    def ensure_vehicle(self, sn: str) -> None:
        if not any(str(vehicle.get("wnumber") or vehicle.get("sn")) == sn for vehicle in self.vehicles()):
            raise KeyError(sn)

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

    def travel(self, sn: str, month: str) -> dict[str, Any]:
        payload = self.run("travel", sn, "--month", month)
        return payload if isinstance(payload, dict) else {"month": month, "list": []}

    def travel_detail(self, sn: str, travel_id: str) -> dict[str, Any]:
        payload = self.run("travel", sn, "--detail", travel_id)
        if not isinstance(payload, dict):
            raise RuntimeError(f"行程 {travel_id} 的详情格式无效")
        return payload

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
        self.account_store = AccountStore(settings) if settings.backend == "direct" and ha is None else None
        self.direct = None
        self.ha = ha or (None if self.account_store else HomeAssistantClient(settings))
        self.config = {"vehicles": []} if self.account_store else self._load_config()
        self._sessions: dict[str, tuple[str, dict[str, Any]]] = {}
        self._clients: dict[str, DirectNinebotClient] = {}
        self._session_lock = threading.RLock()
        self._admin_sessions: set[str] = set()

    def login(self, account: str, password: str) -> dict[str, Any] | None:
        if not self.account_store:
            configured_account = self.settings.account
            configured_password = self.settings.password
            if configured_account and account != configured_account:
                return None
            if configured_password and password != configured_password:
                return None
            record: dict[str, Any] = {}
        else:
            record = self.account_store.authenticate(account, password) or {}
            if not record:
                return None
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[token] = (account, record)
        return {"phone": account, "session_token": token}

    def client_for_session(self, token: str) -> DirectNinebotClient | None:
        if not self.account_store:
            return None
        with self._session_lock:
            session = self._sessions.get(token)
            if not session:
                return None
            account, record = session
            client = self._clients.get(account)
            if client is None:
                client = DirectNinebotClient(self.account_store.client_settings(record))
                self._clients[account] = client
            return client

    def new_admin_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._admin_sessions.add(token)
        return token

    def is_admin_session(self, token: str) -> bool:
        with self._session_lock:
            return token in self._admin_sessions

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

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if "application/x-www-form-urlencoded" in self.headers.get("Content-Type", ""):
            values = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            return {key: items[-1] for key, items in values.items()}
        return json.loads(raw)

    def _reply(self, status: int, data: Any = None, error: str | None = None) -> None:
        payload = {"ok": error is None}
        payload["data" if error is None else "error"] = data if error is None else {"message": error}
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _html_reply(self, status: int, document: str, cookie: str | None = None) -> None:
        encoded = document.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(encoded)

    def _admin_token(self) -> str:
        cookies = self.headers.get("Cookie", "")
        for item in cookies.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == "nineplus_admin":
                return value
        return ""

    def _admin_page(self, message: str = "", error: str = "") -> str:
        rows = ""
        if self.adapter.account_store:
            for account in self.adapter.account_store.list_accounts():
                rows += (
                    "<tr><td>" + html.escape(str(account["account"])) + "</td><td>" +
                    html.escape(str(account["ninebot_username"])) + "</td><td>" +
                    html.escape(str(account["created_at"])) + "</td></tr>"
                )
        notice = f'<p class="ok">{html.escape(message)}</p>' if message else ""
        notice += f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NinePlus 账号管理</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f4f6f8;color:#17202a}}main{{max-width:820px;margin:40px auto;padding:0 18px}}section{{background:white;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 8px 28px #0000000d}}h1,h2{{margin-top:0}}label{{display:block;font-size:14px;margin:13px 0 5px}}input{{box-sizing:border-box;width:100%;padding:11px;border:1px solid #ccd2d8;border-radius:9px;font-size:16px}}button{{margin-top:18px;padding:11px 18px;border:0;border-radius:9px;background:#14181c;color:white;font-size:15px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px 8px;text-align:left;border-bottom:1px solid #e8ebee;font-size:14px}}.ok{{color:#16803c}}.error{{color:#c9342f}}.hint{{color:#66717c;font-size:13px}}</style></head><body><main><h1>NinePlus 账号管理</h1>{notice}<section><h2>新增账号</h2><form method="post" action="/admin/accounts"><label>NineBot+ 登录账号</label><input name="app_account" autocomplete="username" required><label>NineBot+ 登录密码</label><input name="app_password" type="password" autocomplete="new-password" minlength="6" required><label>九号出行账号</label><input name="ninebot_username" required><label>九号出行密码</label><input name="ninebot_password" type="password" autocomplete="off" required><p class="hint">九号密码仅用于本次登录换取令牌，不写入 accounts.json。</p><button type="submit">验证九号账号并新增</button></form></section><section><h2>已有账号</h2><table><thead><tr><th>NineBot+ 账号</th><th>九号账号</th><th>创建时间</th></tr></thead><tbody>{rows or '<tr><td colspan="3">暂无账号</td></tr>'}</tbody></table></section></main></body></html>"""

    def _admin_login_page(self, error: str = "") -> str:
        notice = f'<p style="color:#c9342f">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NinePlus 后台登录</title></head><body style="font-family:-apple-system,sans-serif;background:#f4f6f8"><main style="max-width:420px;margin:80px auto;background:white;padding:28px;border-radius:16px"><h1>NinePlus 后台</h1>{notice}<form method="post" action="/admin/login"><label>管理员密码</label><input name="password" type="password" required style="box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ccd2d8;border-radius:9px"><button style="padding:11px 18px;border:0;border-radius:9px;background:#14181c;color:white">登录</button></form></main></body></html>"""

    def _authorized(self) -> bool:
        required = self.adapter.settings.bearer_token
        return not required or self.headers.get("Authorization") == f"Bearer {required}"

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        method = self.command
        body = self._body() if method == "POST" else {}

        if parts == ["admin", "login"] and method == "POST":
            if not self.adapter.settings.admin_password:
                self._html_reply(HTTPStatus.SERVICE_UNAVAILABLE, self._admin_login_page("请先配置 NINEPLUS_ADMIN_PASSWORD"))
                return
            if not hmac.compare_digest(str(body.get("password", "")), self.adapter.settings.admin_password):
                self._html_reply(HTTPStatus.UNAUTHORIZED, self._admin_login_page("管理员密码错误"))
                return
            token = self.adapter.new_admin_session()
            self._html_reply(HTTPStatus.SEE_OTHER, '<meta http-equiv="refresh" content="0;url=/admin">', f"nineplus_admin={token}; Path=/admin; HttpOnly; SameSite=Strict")
            return
        if parts == ["admin"] and method == "GET":
            if not self.adapter.is_admin_session(self._admin_token()):
                self._html_reply(HTTPStatus.OK, self._admin_login_page())
            else:
                query = urllib.parse.parse_qs(parsed.query)
                self._html_reply(HTTPStatus.OK, self._admin_page(query.get("message", [""])[0], query.get("error", [""])[0]))
            return
        if parts == ["admin", "accounts"] and method == "POST":
            if not self.adapter.is_admin_session(self._admin_token()):
                self._html_reply(HTTPStatus.UNAUTHORIZED, self._admin_login_page("登录已失效"))
                return
            if not self.adapter.account_store:
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_page(error="仅直连模式支持多账号"))
                return
            try:
                result = self.adapter.account_store.add_account(
                    str(body.get("app_account", "")), str(body.get("app_password", "")),
                    str(body.get("ninebot_username", "")), str(body.get("ninebot_password", "")),
                )
                message = f"账号添加成功，发现 {result['vehicle_count']} 辆车"
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/admin?" + urllib.parse.urlencode({"message": message}))
                self.end_headers()
            except (RuntimeError, ValueError) as exc:
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_page(error=str(exc)))
            return

        if parts == ["healthz"] and method == "GET":
            if self.adapter.account_store:
                backend = "ninebot-cloud-multi-account"
                account_count = len(self.adapter.account_store.list_accounts())
            else:
                self.adapter.ha.check()
                backend = "home-assistant"
                account_count = 1
            self._reply(HTTPStatus.OK, {"status": "ok", "backend": backend, "accounts": account_count})
            return
        if not self._authorized():
            self._reply(HTTPStatus.UNAUTHORIZED, error="Bearer Token 无效")
            return
        if parts == ["accounts", "login"] and method == "POST":
            result = self.adapter.login(str(body.get("account", "")), str(body.get("password", "")))
            if not result:
                self._reply(HTTPStatus.UNAUTHORIZED, error="账号或密码错误")
                return
            self._reply(HTTPStatus.OK, result)
            return
        direct_client = None
        if self.adapter.account_store:
            direct_client = self.adapter.client_for_session(self.headers.get("X-NinePlus-Session", ""))
            if direct_client is None:
                self._reply(HTTPStatus.UNAUTHORIZED, error="登录会话无效，请重新登录")
                return
        if parts == ["vehicles"] and method == "GET":
            vehicles = direct_client.vehicles() if direct_client else [self.adapter.vehicle_info(v) for v in self.adapter.config["vehicles"]]
            self._reply(HTTPStatus.OK, {"vehicles": vehicles})
            return
        if len(parts) >= 3 and parts[0] == "vehicles":
            sn, endpoint = parts[1], parts[2]
            if direct_client:
                direct_client.ensure_vehicle(sn)
            if method == "GET" and endpoint in {"dashboard", "status", "battery"}:
                dashboard = direct_client.dashboard(sn) if direct_client else self.adapter.dashboard(sn)
                status_key = "status" if direct_client else "state"
                value = dashboard if endpoint == "dashboard" else dashboard[status_key if endpoint == "status" else "battery"]
                self._reply(HTTPStatus.OK, value)
                return
            if method == "GET" and endpoint == "travel" and len(parts) == 4:
                travel_id = urllib.parse.unquote(parts[3])
                if direct_client:
                    self._reply(HTTPStatus.OK, direct_client.travel_detail(sn, travel_id))
                else:
                    self._reply(HTTPStatus.NOT_IMPLEMENTED, error="HA 模式不提供行程详情")
                return
            if method == "GET" and endpoint == "travel":
                month = urllib.parse.parse_qs(parsed.query).get("month", [datetime.now().strftime("%Y%m")])[0]
                value = direct_client.travel(sn, month) if direct_client else {"month": month, "list": [], "total": 0}
                self._reply(HTTPStatus.OK, value)
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
                self._reply(HTTPStatus.OK, direct_client.action(sn, action) if direct_client else self.adapter.action(sn, action))
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
    if settings.backend == "direct" and not settings.admin_password:
        raise SystemExit("直连模式下 NINEPLUS_ADMIN_PASSWORD 不能为空")
    Handler.adapter = NinePlusAdapter(settings)
    host = _env("HOST", "0.0.0.0")
    port = int(_env("PORT", "19009"))
    print(f"NinePlus HA adapter listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
