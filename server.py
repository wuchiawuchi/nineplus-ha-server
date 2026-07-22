#!/usr/bin/env python3
"""NinePlus-compatible HTTP adapter backed by the Ninebot cloud."""

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
import urllib.parse
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


@dataclass(frozen=True)
class Settings:
    bearer_token: str
    account: str = ""
    password: str = ""
    ninebot_username: str = ""
    ninebot_password: str = ""
    ninebot_config_dir: Path = Path("/data/ninebot")
    accounts_path: Path = Path("/data/ninebot/accounts.json")
    admin_password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bearer_token=_env("NINEPLUS_BEARER_TOKEN"),
            account=_env("NINEPLUS_ACCOUNT"),
            password=_secret_env("NINEPLUS_PASSWORD_B64", "NINEPLUS_PASSWORD"),
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
        self._meta: dict[str, Any] = {}
        self._accounts = self._load()
        self._migrate_legacy_account()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and isinstance(payload.get("meta"), dict):
            self._meta = dict(payload["meta"])
        values = payload.get("accounts", payload) if isinstance(payload, dict) else {}
        if not isinstance(values, dict):
            raise RuntimeError("accounts.json 格式无效")
        return {str(key): value for key, value in values.items() if isinstance(value, dict)}

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": 1, "meta": self._meta, "accounts": self._accounts}, handle, ensure_ascii=False, indent=2)
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

    @staticmethod
    def _is_mobile_phone(value: str) -> bool:
        return len(value) == 11 and value.isascii() and value.isdigit() and value[0] == "1" and value[1] in "3456789"

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

    def admin_configured(self) -> bool:
        return bool(self.settings.admin_password or self._meta.get("admin_password_hash"))

    def authenticate_admin(self, password: str) -> bool:
        salt = str(self._meta.get("admin_password_salt", ""))
        digest = str(self._meta.get("admin_password_hash", ""))
        if salt and digest:
            _, candidate = self._password_hash(password, salt)
            return hmac.compare_digest(candidate, digest)
        return bool(self.settings.admin_password) and hmac.compare_digest(password, self.settings.admin_password)

    def setup_admin(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("管理员密码至少需要 8 位")
        with self._lock:
            if self.admin_configured():
                raise ValueError("管理员密码已经设置")
            salt, digest = self._password_hash(password)
            self._meta["admin_password_salt"] = salt
            self._meta["admin_password_hash"] = digest
            self._save()

    def change_admin_password(self, current_password: str, new_password: str) -> None:
        if not self.authenticate_admin(current_password):
            raise ValueError("当前管理员密码错误")
        if len(new_password) < 8:
            raise ValueError("新管理员密码至少需要 8 位")
        with self._lock:
            salt, digest = self._password_hash(new_password)
            self._meta["admin_password_salt"] = salt
            self._meta["admin_password_hash"] = digest
            self._save()

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
        if not self._is_mobile_phone(app_account):
            raise ValueError("NineBot+ 账号必须是有效的手机号")
        if len(app_password) < 8:
            raise ValueError("NineBot+ 密码至少需要 8 位")
        if not self._is_mobile_phone(ninebot_username):
            raise ValueError("九号出行账号必须是有效的手机号")
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
            self.settings.bearer_token, self.settings.account, self.settings.password,
            ninebot_username=username, ninebot_password=password,
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


class NinePlusAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.account_store = AccountStore(settings)
        self._sessions: dict[str, tuple[str, dict[str, Any]]] = {}
        self._clients: dict[str, DirectNinebotClient] = {}
        self._session_lock = threading.RLock()
        self._admin_sessions: set[str] = set()

    def login(self, account: str, password: str) -> dict[str, Any] | None:
        record = self.account_store.authenticate(account, password) or {}
        if not record:
            return None
        token = secrets.token_urlsafe(32)
        with self._session_lock:
            self._sessions[token] = (account, record)
        return {"phone": account, "session_token": token}

    def client_for_session(self, token: str) -> DirectNinebotClient | None:
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

    def admin_configured(self) -> bool:
        return self.account_store.admin_configured()

    def authenticate_admin(self, password: str) -> bool:
        return self.account_store.authenticate_admin(password)

    def setup_admin(self, password: str) -> None:
        self.account_store.setup_admin(password)

    def change_admin_password(self, current_password: str, new_password: str) -> None:
        self.account_store.change_admin_password(current_password, new_password)
        with self._session_lock:
            self._admin_sessions.clear()

    def is_admin_session(self, token: str) -> bool:
        with self._session_lock:
            return token in self._admin_sessions

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
        content = """<section><h2>新增账号</h2><form method="post" action="/admin/accounts" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='正在验证九号账号，请稍候…';"><label>NineBot+ 登录账号（手机号）</label><input name="app_account" type="tel" inputmode="numeric" autocomplete="username" pattern="1[3-9][0-9]{9}" maxlength="11" title="请输入 11 位手机号" required><label>NineBot+ 登录密码（至少 8 位）</label><input name="app_password" type="password" autocomplete="new-password" minlength="8" required><label>九号出行账号（手机号）</label><input name="ninebot_username" type="tel" inputmode="numeric" autocomplete="username" pattern="1[3-9][0-9]{9}" maxlength="11" title="请输入 11 位手机号" required><label>九号出行密码</label><input name="ninebot_password" type="password" autocomplete="off" required><p class="hint">九号密码仅用于本次登录换取令牌，不写入 accounts.json。</p><button type="submit">验证九号账号并新增</button></form></section>"""
        return self._admin_layout("新增账号", content, message, error)

    def _admin_accounts_page(self, message: str = "", error: str = "") -> str:
        rows = ""
        for account in self.adapter.account_store.list_accounts():
            rows += (
                "<tr><td>" + html.escape(str(account["account"])) + "</td><td>" +
                html.escape(str(account["ninebot_username"])) + "</td><td>" +
                html.escape(str(account["created_at"])) + "</td></tr>"
            )
        content = f"""<section><h2>已有账号</h2><table><thead><tr><th>NineBot+ 账号</th><th>九号账号</th><th>创建时间</th></tr></thead><tbody>{rows or '<tr><td colspan="3">暂无账号</td></tr>'}</tbody></table></section>"""
        return self._admin_layout("已有账号", content, message, error)

    def _admin_password_page(self, message: str = "", error: str = "") -> str:
        content = """<section><h2>修改管理员密码</h2><form method="post" action="/admin/password" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='正在修改，请稍候…';"><label>当前密码</label><input name="current_password" type="password" autocomplete="current-password" required><label>新密码（至少 8 位）</label><input name="new_password" type="password" autocomplete="new-password" minlength="8" required><label>再次输入新密码</label><input name="new_password_confirm" type="password" autocomplete="new-password" minlength="8" required><button type="submit">修改管理员密码</button></form></section>"""
        return self._admin_layout("修改管理员密码", content, message, error)

    @staticmethod
    def _admin_layout(title: str, content: str, message: str = "", error: str = "") -> str:
        notice = f'<p class="ok">{html.escape(message)}</p>' if message else ""
        notice += f'<p class="error">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NinePlus 账号管理</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;background:#f4f6f8;color:#17202a}}main{{max-width:820px;margin:40px auto;padding:0 18px}}nav{{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px}}nav a{{padding:10px 14px;border-radius:9px;background:white;color:#17202a;text-decoration:none;box-shadow:0 3px 12px #0000000a}}nav a.active{{background:#14181c;color:white}}section{{background:white;border-radius:16px;padding:22px;margin-bottom:18px;box-shadow:0 8px 28px #0000000d}}h1,h2{{margin-top:0}}label{{display:block;font-size:14px;margin:13px 0 5px}}input{{box-sizing:border-box;width:100%;padding:11px;border:1px solid #ccd2d8;border-radius:9px;font-size:16px}}button{{margin-top:18px;padding:11px 18px;border:0;border-radius:9px;background:#14181c;color:white;font-size:15px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:10px 8px;text-align:left;border-bottom:1px solid #e8ebee;font-size:14px}}.ok{{color:#16803c}}.error{{color:#c9342f}}.hint{{color:#66717c;font-size:13px}}</style></head><body><main><h1>NinePlus 账号管理</h1><nav><a class="{'active' if title == '新增账号' else ''}" href="/admin">新增账号</a><a class="{'active' if title == '已有账号' else ''}" href="/admin/accounts">已有账号</a><a class="{'active' if title == '修改管理员密码' else ''}" href="/admin/password">修改管理员密码</a></nav>{notice}{content}</main></body></html>"""

    def _admin_login_page(self, error: str = "", message: str = "") -> str:
        notice = f'<p style="color:#16803c">{html.escape(message)}</p>' if message else ""
        notice += f'<p style="color:#c9342f">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NinePlus 后台登录</title></head><body style="font-family:-apple-system,sans-serif;background:#f4f6f8"><main style="max-width:420px;margin:80px auto;background:white;padding:28px;border-radius:16px"><h1>NinePlus 后台</h1>{notice}<form method="post" action="/admin/login"><label>管理员密码</label><input name="password" type="password" required style="box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ccd2d8;border-radius:9px"><button style="padding:11px 18px;border:0;border-radius:9px;background:#14181c;color:white">登录</button></form></main></body></html>"""

    def _admin_setup_page(self, error: str = "") -> str:
        notice = f'<p style="color:#c9342f">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>初始化 NinePlus 后台</title></head><body style="font-family:-apple-system,sans-serif;background:#f4f6f8"><main style="max-width:420px;margin:80px auto;background:white;padding:28px;border-radius:16px"><h1>初始化后台</h1><p>第一次使用，请设置管理员密码。密码至少 8 位。</p>{notice}<form method="post" action="/admin/setup"><label>设置管理员密码</label><input name="password" type="password" required minlength="8" style="box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ccd2d8;border-radius:9px"><label>再次输入</label><input name="password_confirm" type="password" required minlength="8" style="box-sizing:border-box;width:100%;padding:12px;margin:8px 0;border:1px solid #ccd2d8;border-radius:9px"><button style="padding:11px 18px;border:0;border-radius:9px;background:#14181c;color:white">保存并进入后台</button></form></main></body></html>"""

    def _authorized(self) -> bool:
        required = self.adapter.settings.bearer_token
        return not required or self.headers.get("Authorization") == f"Bearer {required}"

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        method = self.command
        body = self._body() if method == "POST" else {}

        if parts == ["admin", "setup"] and method == "POST":
            if self.adapter.admin_configured():
                self._html_reply(HTTPStatus.SEE_OTHER, '<meta http-equiv="refresh" content="0;url=/admin">')
                return
            if body.get("password") != body.get("password_confirm"):
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_setup_page("两次输入的密码不一致"))
                return
            try:
                self.adapter.setup_admin(str(body.get("password", "")))
                token = self.adapter.new_admin_session()
                self._html_reply(HTTPStatus.SEE_OTHER, '<meta http-equiv="refresh" content="0;url=/admin">', f"nineplus_admin={token}; Path=/admin; HttpOnly; SameSite=Strict")
            except ValueError as exc:
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_setup_page(str(exc)))
            return
        if parts == ["admin", "login"] and method == "POST":
            if not self.adapter.admin_configured():
                self._html_reply(HTTPStatus.OK, self._admin_setup_page())
                return
            if not self.adapter.authenticate_admin(str(body.get("password", ""))):
                self._html_reply(HTTPStatus.UNAUTHORIZED, self._admin_login_page("管理员密码错误"))
                return
            token = self.adapter.new_admin_session()
            self._html_reply(HTTPStatus.SEE_OTHER, '<meta http-equiv="refresh" content="0;url=/admin">', f"nineplus_admin={token}; Path=/admin; HttpOnly; SameSite=Strict")
            return
        if parts in (["admin"], ["admin", "accounts"], ["admin", "password"]) and method == "GET":
            if not self.adapter.is_admin_session(self._admin_token()):
                page = self._admin_login_page() if self.adapter.admin_configured() else self._admin_setup_page()
                self._html_reply(HTTPStatus.OK, page)
            else:
                query = urllib.parse.parse_qs(parsed.query)
                message = query.get("message", [""])[0]
                error = query.get("error", [""])[0]
                page = self._admin_page(message, error)
                if parts == ["admin", "accounts"]:
                    page = self._admin_accounts_page(message, error)
                elif parts == ["admin", "password"]:
                    page = self._admin_password_page(message, error)
                self._html_reply(HTTPStatus.OK, page)
            return
        if parts == ["admin", "password"] and method == "POST":
            if not self.adapter.is_admin_session(self._admin_token()):
                self._html_reply(HTTPStatus.UNAUTHORIZED, self._admin_login_page("登录已失效"))
                return
            if body.get("new_password") != body.get("new_password_confirm"):
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_password_page(error="两次输入的新密码不一致"))
                return
            try:
                self.adapter.change_admin_password(
                    str(body.get("current_password", "")), str(body.get("new_password", "")),
                )
                self._html_reply(
                    HTTPStatus.OK,
                    self._admin_login_page(message="密码修改成功，请使用新密码登录"),
                    "nineplus_admin=; Path=/admin; HttpOnly; SameSite=Strict; Max-Age=0",
                )
            except ValueError as exc:
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_password_page(error=str(exc)))
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
                message = f"九号账号验证成功，NineBot+ 账号新增成功，发现 {result['vehicle_count']} 辆车"
                self._html_reply(HTTPStatus.OK, self._admin_accounts_page(message=message))
            except (RuntimeError, ValueError) as exc:
                self._html_reply(HTTPStatus.BAD_REQUEST, self._admin_page(error=str(exc)))
            return

        if parts == ["healthz"] and method == "GET":
            backend = "ninebot-cloud-multi-account"
            account_count = len(self.adapter.account_store.list_accounts())
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
        direct_client = self.adapter.client_for_session(self.headers.get("X-NinePlus-Session", ""))
        if direct_client is None:
            self._reply(HTTPStatus.UNAUTHORIZED, error="登录会话无效，请重新登录")
            return
        if parts == ["vehicles"] and method == "GET":
            self._reply(HTTPStatus.OK, {"vehicles": direct_client.vehicles()})
            return
        if len(parts) >= 3 and parts[0] == "vehicles":
            sn, endpoint = parts[1], parts[2]
            if direct_client:
                direct_client.ensure_vehicle(sn)
            if method == "GET" and endpoint in {"dashboard", "status", "battery"}:
                dashboard = direct_client.dashboard(sn)
                value = dashboard if endpoint == "dashboard" else dashboard["status" if endpoint == "status" else "battery"]
                self._reply(HTTPStatus.OK, value)
                return
            if method == "GET" and endpoint == "travel" and len(parts) == 4:
                travel_id = urllib.parse.unquote(parts[3])
                self._reply(HTTPStatus.OK, direct_client.travel_detail(sn, travel_id))
                return
            if method == "GET" and endpoint == "travel":
                month = urllib.parse.parse_qs(parsed.query).get("month", [datetime.now().strftime("%Y%m")])[0]
                value = direct_client.travel(sn, month)
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
                self._reply(HTTPStatus.OK, direct_client.action(sn, action))
                return
        if method == "POST" and tuple(parts) in {("devices", "register"), ("live-activities", "register")}:
            self._reply(HTTPStatus.OK, {"accepted": False, "reason": "APNs is not configured"})
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
    Handler.adapter = NinePlusAdapter(settings)
    host = _env("HOST", "0.0.0.0")
    port = int(_env("PORT", "19009"))
    print(f"NinePlus server listening on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
