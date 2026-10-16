"""本地账号库和独立 Chrome 登录窗口；此模块不执行签到。"""

from __future__ import annotations

import contextlib
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from fib_storage import atomic_json, data_root, file_lock


USER_DIR = data_root() / ".users"
TZ = ZoneInfo("Asia/Shanghai")


class UserError(Exception):
    pass


class UserBusy(UserError):
    pass


class LoginCancelled(UserError):
    pass


@contextlib.contextmanager
def store_lock():
    try:
        with file_lock(USER_DIR / "users.lock"):
            yield
    except BlockingIOError:
        raise UserBusy("账号库正在被另一个进程修改，请稍后再试。") from None


def _load():
    path = USER_DIR / "users.json"
    if not path.exists():
        return {"version": 1, "users": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or not isinstance(data.get("users"), dict):
            raise ValueError()
        for account, user in data["users"].items():
            if not isinstance(account, str) or not isinstance(user, dict):
                raise ValueError()
            if not isinstance(user.get("name"), str) or not isinstance(user.get("cookies"), list):
                raise ValueError()
        return data
    except (ValueError, AttributeError, TypeError):
        raise UserError("本地账号库格式异常，请保留原文件并检查，不能覆盖为空账号库。") from None


def _write(data):
    atomic_json(USER_DIR / "users.json", data)


def list_users():
    with store_lock():
        return _load()["users"]


def resolve_user(users, selector):
    named = [account for account, user in users.items() if user["name"].casefold() == selector.casefold()]
    matches = named or [account for account in users if len(selector) >= 6 and account.startswith(selector)]
    if len(matches) != 1:
        raise UserError("找不到唯一匹配的账号，请先执行 list users 查看账号名称或 ID。")
    return matches[0]


def validate_name(name):
    name = name.strip()
    if not name or len(name) > 80 or not all(c.isprintable() for c in name):
        raise UserError("账号名称须为 1～80 个可显示字符。")
    return name


def serialize_cookies(jar):
    return [{
        "name": cookie.name, "value": cookie.value, "domain": cookie.domain,
        "path": cookie.path, "secure": cookie.secure, "expires": cookie.expires,
        "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
    } for cookie in jar]


def deserialize_cookies(items):
    jar = requests.cookies.RequestsCookieJar()
    try:
        for item in items:
            expires = item.get("expires")
            # Playwright 使用 -1 表示会话 Cookie；requests 使用 None。
            expires = int(expires) if expires is not None and expires > 0 else None
            cookie = requests.cookies.create_cookie(
                name=item["name"], value=item["value"], domain=item["domain"],
                path=item.get("path", "/"), secure=bool(item.get("secure", True)),
                expires=expires, rest={"HttpOnly": None} if item.get("httpOnly") else {},
            )
            jar.set_cookie(cookie)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UserError("账号会话格式异常，请重新添加该账号。") from None
    return jar


def save_user(account, jar, name=None, label=""):
    with store_lock():
        data = _load()
        users = data["users"]
        existing = users.get(account)
        fallback = f"user-{account[:8]}"
        old_name = (existing or {}).get("name")
        if old_name == fallback and label:
            old_name = None
        name = validate_name(name or old_name or label or fallback)
        if any(key != account and value["name"].casefold() == name.casefold() for key, value in users.items()):
            raise UserError("该名称已属于另一个 FIB 账号，请换一个名称。")
        now = datetime.now(TZ).isoformat()
        users[account] = {
            "name": name, "email": label if "@" in label else (existing or {}).get("email", ""),
            "created_at": (existing or {}).get("created_at", now), "updated_at": now,
            "cookies": serialize_cookies(jar),
        }
        _write(data)
        return name, existing is not None


def refresh_user(account, jar, expected_updated_at=None):
    # 批量执行期间若账号已被删除，不得重新创建它。
    with store_lock():
        data = _load()
        if account in data["users"]:
            # 不覆盖批量执行期间通过 add user 更新的新会话。
            if expected_updated_at is not None and data["users"][account].get("updated_at") != expected_updated_at:
                return
            data["users"][account]["cookies"] = serialize_cookies(jar)
            data["users"][account]["updated_at"] = datetime.now(TZ).isoformat()
            _write(data)


def delete_user(selector):
    with store_lock():
        data = _load()
        account = resolve_user(data["users"], selector)
        name = data["users"].pop(account)["name"]
        _write(data)
        return name


def rename_user(selector, new_name):
    new_name = validate_name(new_name)
    with store_lock():
        data = _load()
        account = resolve_user(data["users"], selector)
        if any(key != account and user["name"].casefold() == new_name.casefold() for key, user in data["users"].items()):
            raise UserError("该名称已被其他账号使用。")
        data["users"][account]["name"] = new_name
        _write(data)
        return new_name


def capture_login(home_url, validate, timeout=600, cancel_event=None):
    """用户在新窗口手动登录，validate 只通过 GET 验证会话。"""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        raise UserError("添加账号需要 playwright，请先安装 requirements.txt 中的依赖。") from None
    expected = urlsplit(home_url)
    # 路由层阻止登录窗口消费签到机会，包括用户误点签到按钮的情况。
    def guard(route):
        target = urlsplit(route.request.url)
        if target.hostname == expected.hostname and target.path.rstrip("/") == "/user/checkin":
            route.abort()
        else:
            route.continue_()

    browser = None
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=False, chromium_sandbox=True, timeout=30000)
            try:
                context = browser.new_context(no_viewport=True, accept_downloads=False, service_workers="block")
                context.route("**/*", guard)
                page = context.new_page()
                print("已打开独立 Chrome 窗口，请手动登录 FIB；登录验证成功后会自动保存并关闭该窗口。", flush=True)
                try:
                    page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
                except Error:
                    if page.is_closed():
                        raise UserError("登录窗口已关闭，未保存账号。") from None
                    print("首次加载未完成，可在登录窗口中刷新页面后继续。", flush=True)
                deadline = time.monotonic() + timeout
                last_fingerprint, checked_at = None, 0.0
                while time.monotonic() < deadline:
                    if cancel_event is not None and cancel_event.is_set():
                        raise LoginCancelled("已取消登录，未保存账号。")
                    if not browser.is_connected() or not context.pages:
                        raise UserError("登录窗口已关闭，未保存账号。")
                    if page.is_closed():
                        page = context.pages[0]
                    cookies = context.cookies([home_url])
                    names = {item["name"] for item in cookies}
                    fingerprint = hashlib.sha256(json.dumps(cookies, sort_keys=True).encode()).digest()
                    now = time.monotonic()
                    if {"uid", "key"} <= names and (fingerprint != last_fingerprint or now - checked_at >= 5):
                        last_fingerprint, checked_at = fingerprint, now
                        try:
                            return validate(deserialize_cookies(cookies))
                        except UserError:
                            pass  # 尚未登录或只读验证暂时失败，等待用户继续。
                    page.wait_for_timeout(500)
                raise UserError("等待登录超时，未保存账号；可重新执行 add user。")
            finally:
                browser.close()
    except Error:
        raise UserError("独立 Chrome 登录窗口无法启动或已关闭，未保存账号。") from None
