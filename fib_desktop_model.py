"""桌面设置、每日调度及只读账号状态模型。"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from fib_storage import atomic_json, data_root, file_lock


TZ = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    "version": 1, "enabled": False, "time": "09:00", "catch_up": True,
    "start_hidden": True, "notifications": True, "last_scheduled_day": "",
}


def validate_settings(settings):
    value = settings.get("time", "")
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("签到时间格式应为 HH:MM。")
    if value < "00:02" or value > "23:57":
        raise ValueError("请将签到时间设在 00:02～23:57 之间，以避开零点日期切换。")
    for key in ("enabled", "catch_up", "start_hidden", "notifications"):
        if not isinstance(settings.get(key), bool):
            raise ValueError("设置格式异常。")
    day = settings.get("last_scheduled_day", "")
    if not isinstance(day, str) or day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("调度记录格式异常。")


class DesktopStore:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else data_root()
        self.settings_path = self.root / "desktop.json"

    def settings(self):
        if not self.settings_path.exists():
            return DEFAULTS.copy()
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if saved.get("version") != 1:
                raise ValueError("设置版本不支持。")
            result = DEFAULTS | saved
            validate_settings(result)
            return result
        except (TypeError, AttributeError, json.JSONDecodeError):
            raise ValueError("桌面设置文件损坏，请检查数据目录。") from None

    def save_settings(self, values):
        with file_lock(self.root / "schedule.lock"):
            # UI 修改设置时保留已落盘的当日调度标记。
            settings = self.settings()
            settings.update({key: values[key] for key in DEFAULTS if key in values and key not in {"version", "last_scheduled_day"}})
            validate_settings(settings)
            atomic_json(self.settings_path, settings)
            return settings

    def claim_due(self, now=None, busy=False, has_users=True):
        now = (now or datetime.now(TZ)).astimezone(TZ)
        if busy or not has_users or not "00:02" <= now.strftime("%H:%M") <= "23:57":
            return False
        with file_lock(self.root / "schedule.lock"):
            settings = self.settings()
            day = now.date().isoformat()
            if not settings["enabled"] or settings["last_scheduled_day"] >= day:
                return False
            current_time = now.strftime("%H:%M")
            if current_time < settings["time"] or not settings["catch_up"] and current_time != settings["time"]:
                return False
            # 先登记再启动批量任务，重启或异常后不自动重复发起当日批次。
            settings["last_scheduled_day"] = day
            atomic_json(self.settings_path, settings)
            return True

    def next_run(self, now=None):
        now = (now or datetime.now(TZ)).astimezone(TZ)
        settings = self.settings()
        if not settings["enabled"]:
            return None
        hour, minute = map(int, settings["time"].split(":"))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if settings["last_scheduled_day"] < now.date().isoformat():
            if now < target:
                return target
            if settings["catch_up"]:
                if now.strftime("%H:%M") < "00:02":
                    return now.replace(hour=0, minute=2, second=0, microsecond=0)
                if now.strftime("%H:%M") <= "23:57":
                    return now
        return target + timedelta(days=1)

    def results(self):
        path = self.root / "desktop-results.json"
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("桌面结果记录格式异常。")
        return data

    def record_result(self, account, code, dry_run=False):
        with file_lock(self.root / "results.lock"):
            results = self.results()
            entry = results.setdefault(account, {})
            stamp = datetime.now(TZ).isoformat()
            entry.update(checked_at=stamp, check_code=code)
            if not dry_run:
                entry["checkin"] = {"day": datetime.now(TZ).date().isoformat(), "code": code, "at": stamp}
            atomic_json(self.root / "desktop-results.json", results)


def account_rows(users, store, state_dir, now=None):
    import fib_checkin as core
    import fib_users

    now = (now or datetime.now(TZ)).astimezone(TZ)
    day = now.date().isoformat()
    results = store.results()
    rows = []
    for account, user in users.items():
        session_status = "会话已保存"
        try:
            _, actual = core.account_cookies(fib_users.deserialize_cookies(user["cookies"]))
            if actual != account:
                session_status = "需要重新登录"
        except (core.SafeStop, fib_users.UserError):
            session_status = "需要重新登录"
        latest = results.get(account, {})
        if latest.get("check_code") == 2 and latest.get("checked_at", "") > user.get("updated_at", ""):
            session_status = "需要检查会话或网络"
        expirations = [item.get("expires") for item in user["cookies"] if item.get("name") in {"uid", "key"} and isinstance(item.get("expires"), (int, float)) and item["expires"] > 0]
        if expirations:
            expiration = datetime.fromtimestamp(min(expirations), TZ)
            session_status += "\n到期 " + expiration.strftime("%m-%d %H:%M")
        if user.get("auto_login"):
            session_status += "\n可自动重新登录"
        today = "尚未处理"
        last_at = latest.get("checked_at", "")
        checkin = latest.get("checkin", {})
        if checkin.get("day") == day:
            today = {0: "已签到", 2: "需要处理", 3: "结果未确认"}.get(checkin.get("code"), "尚未处理")
        record_path = Path(state_dir) / f"{account}-{day}.json"
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            today = "已签到" if record.get("status") in {"verified_success", "server_confirmed"} else "已提交 · 未确认"
            last_at = record.get("verified_at") or record.get("started_at") or last_at
        rows.append({"id": account, "name": user["name"], "session": session_status, "today": today, "last_at": last_at})
    return rows
