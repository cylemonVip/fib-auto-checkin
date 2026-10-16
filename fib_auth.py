"""会话失效时恢复登录；本模块只允许调用登录和只读验证，绝不签到。"""

from datetime import datetime
import hashlib
import json
import re

from bs4 import BeautifulSoup
import requests

import fib_credentials as credentials
import fib_users as users
from fib_storage import atomic_json


LOGIN_SCRIPT_SHA256 = "8ec31f8c4112f5488377b8a0bc9f6cf2fe3b38fc38117682fb112dc249d30e93"
COOLDOWN_SECONDS = 1800


def validate_credentials(email, password):
    email = email.strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 320:
        raise users.UserError("请填写有效的 FIB 登录邮箱。")
    if not isinstance(password, str) or not password or len(password) > 1024:
        raise users.UserError("请输入密码（最多 1024 个字符）。")
    return email


def login(email, password, account):
    import fib_checkin as core
    email = validate_credentials(email, password)
    url = core.SITE + "/auth/login"
    try:
        with core.new_session(requests.cookies.RequestsCookieJar()) as session:
            response = session.get(url, timeout=core.TIMEOUT, allow_redirects=False)
            if response.status_code != 200:
                raise users.UserError("登录页面不可用，请稍后手动重新登录。")
            soup = BeautifulSoup(response.text, "html.parser")
            scripts = [script.get_text() for script in soup.find_all("script") if "remember_me" in script.get_text()]
            if len(scripts) != 1 or hashlib.sha256(scripts[0].encode()).hexdigest() != LOGIN_SCRIPT_SHA256:
                raise users.UserError("网站登录流程已变化，自动登录已停止；请使用 Chrome 手动登录。")
            result = session.post(url, data={"email": email, "passwd": password, "code": "", "remember_me": "on"},
                                  headers={"Referer": url, "Origin": core.SITE, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                                  timeout=core.TIMEOUT, allow_redirects=False)
            if result.status_code != 200:
                raise users.UserError("网站拒绝登录或需要人工验证，请使用 Chrome 重新登录。")
            try:
                data = result.json()
            except ValueError:
                raise users.UserError("登录响应无法确认，请使用 Chrome 重新登录。") from None
            if not isinstance(data, dict):
                raise users.UserError("登录响应无法确认，请使用 Chrome 重新登录。")
            if data.get("ret") in (2, "2"):
                raise users.UserError("网站要求动态令牌，请使用 Chrome 手动完成两步验证。")
            if data.get("ret") not in (1, "1", True):
                raise users.UserError("自动登录失败，请检查邮箱和密码，或使用 Chrome 完成人工验证。")
            jar, actual = core.account_cookies(session.cookies)
            if actual != account:
                raise users.UserError("登录邮箱对应另一个 FIB 账号，未保存密码或覆盖原会话。")
            jar, actual, _ = core.validate_login(jar)
            if actual != account:
                raise users.UserError("登录验证账号不一致，已停止。")
            return jar
    except requests.RequestException:
        raise users.UserError("登录网络请求失败，未自动重试；没有提交签到请求。") from None
    except core.SafeStop:
        raise users.UserError("登录会话验证未通过，请使用 Chrome 重新登录。") from None


def configure(account, email, password):
    """用户主动验证一次密码；成功后才启用恢复，始终不签到。"""
    import fib_checkin as core
    email = validate_credentials(email, password)
    credentials.backend()  # 在发送登录前先确认系统凭据后端可用。
    with core.account_lock(account):
        if account not in users.list_users():
            raise users.UserError("账号已被移除，未保存密码。")
        jar = login(email, password, account)
        credentials.save(account, email, password)
        users.configure_auto_login(account, True, email, jar)
        path = users.USER_DIR / "auth" / f"{account}.json"
        atomic_json(path, {"attempted_at": datetime.now(users.TZ).isoformat(), "status": "success"})


def disable(account):
    import fib_checkin as core
    with core.account_lock(account):
        credentials.delete(account)
        users.configure_auto_login(account, False)


def restore(account):
    """一次恢复尝试；失败或超时后 30 分钟内不再自动提交登录。"""
    import fib_checkin as core
    with core.account_lock(account):
        current = users.list_users().get(account)
        if not current or not current.get("auto_login"):
            raise users.UserError("会话失效且未启用自动登录，请重新登录或配置自动登录。")
        path = users.USER_DIR / "auth" / f"{account}.json"
        now = datetime.now(users.TZ)
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                elapsed = (now - datetime.fromisoformat(previous["attempted_at"])).total_seconds()
            except (ValueError, KeyError, TypeError):
                raise users.UserError("自动登录记录异常，请重新设置自动登录。") from None
            if elapsed < COOLDOWN_SECONDS:
                raise users.UserError("近期已尝试登录，30 分钟内不会再次自动尝试；请检查凭据或手动重新登录。")
        email, password = credentials.load(account)
        if email != current.get("login_email"):
            raise users.UserError("自动登录凭据与本地配置不一致，请重新设置。")
        record = {"attempted_at": now.isoformat(), "status": "pending"}
        atomic_json(path, record)
        try:
            jar = login(email, password, account)
            users.configure_auto_login(account, True, email, jar)
            record["status"] = "success"
        except Exception:
            record["status"] = "failed"
            raise
        finally:
            password = None
            atomic_json(path, record)
        refreshed = users.list_users().get(account)
        if refreshed is None:
            raise users.UserError("账号已被移除，已停止。")
        return refreshed
