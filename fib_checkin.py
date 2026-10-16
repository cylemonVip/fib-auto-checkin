#!/Users/mac/Documents/constract/venv/bin/python
"""FIB 多账号签到：添加/删除本地账号，后台批量签到，每个账号每天最多提交一次。"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from fib_storage import atomic_json, data_root, file_lock, initialize_data

try:
    import browser_cookie3
    import requests
    from bs4 import BeautifulSoup
    import fib_users
except ImportError:
    sys.exit("缺少依赖，请执行：/Users/mac/Documents/constract/venv/bin/python -m pip install -r requirements.txt")


SITE = "https://6.fib-acc.net"
HOST = "6.fib-acc.net"
HOME_URL = SITE + "/user"
CHECKIN_URL = SITE + "/user/checkin"
SCRIPT_URL = SITE + "/theme/cool/assets/js/cool.js?6"
# 已离线解析此版本：空正文 POST /user/checkin，无验证码或额外请求参数。
SCRIPT_SHA256 = "f5959539df91d40eb097e5f7c262ded323f02fa1ef761eca402ae4bfaee568f8"
if sys.platform == "win32":
    CHROME_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Google/Chrome/User Data"
else:
    CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
STATE_DIR = data_root() / ".state"
TZ = ZoneInfo("Asia/Shanghai")
TIMEOUT = (10, 30)


class SafeStop(Exception):
    """预检查未通过，此时不能提交签到。"""


def chrome_cookies(profile: str):
    if Path(profile).name != profile or profile in {".", ".."}:
        raise SafeStop("Chrome 配置名称无效。")
    candidates = [CHROME_ROOT / profile / "Cookies", CHROME_ROOT / profile / "Network/Cookies"]
    cookie_file = next((p for p in candidates if p.is_file()), None)
    if cookie_file is None:
        raise SafeStop(f"找不到 Chrome 配置 {profile!r} 的 Cookies 文件。")
    try:
        original = browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name=HOST)
    except Exception:
        raise SafeStop("无法读取 Chrome 登录态；请检查 macOS 钥匙串授权和 Chrome 配置。") from None
    return account_cookies(original)


def account_cookies(original):
    # 限定域名和当前请求路径；不打印或发送其他网站的 Cookie。
    jar = requests.cookies.RequestsCookieJar()
    for cookie in original:
        domain = cookie.domain.lstrip(".")
        if (HOST == domain or HOST.endswith("." + domain)) and not cookie.is_expired():
            jar.set_cookie(cookie)
    prepared = requests.Request("GET", HOME_URL).prepare()
    selected = requests.cookies.get_cookie_header(jar, prepared) or ""
    names = {part.split("=", 1)[0].strip() for part in selected.split(";") if "=" in part}
    uids = {c.value for c in jar if c.name == "uid" and c.value}
    if not {"uid", "key"} <= names or len(uids) != 1:
        raise SafeStop("没有有效的 FIB 登录态，请通过 add user 重新登录该账号。")
    account = hashlib.sha256((SITE + "\0" + next(iter(uids))).encode()).hexdigest()[:20]
    return jar, account


def new_session(cookies):
    session = requests.Session()
    # 禁用 HTTP 层重试；POST 也禁用重定向，任何异常都不能补发。
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.cookies.update(cookies)
    return session


def page_status(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if "FIB ACCELERATE" not in title or "用户中心" not in title:
        raise SafeStop("未收到 FIB 用户中心，登录可能已失效。")
    heading = soup.find("h3", string=lambda s: bool(s and s.strip() == "节点订阅链接"))
    header = heading.find_parent(class_="card-header") if heading else None
    if header is None:
        raise SafeStop("页面结构已变化，无法可靠判断签到状态。")
    text = header.get_text(" ", strip=True)
    if any(word in text for word in ("已签到", "已经签到")) and "未签到" not in text:
        return "done"
    button = header.find("button", id="checkin")
    if button and button.get_text("", strip=True).replace("\xa0", "") == "每日签到" and not button.has_attr("disabled"):
        return "ready"
    raise SafeStop("签到按钮状态不明确，已停止，未提交签到。")


def read_home(session):
    response = session.get(HOME_URL, timeout=TIMEOUT, allow_redirects=False)
    if response.status_code != 200:
        raise SafeStop(f"用户中心返回 HTTP {response.status_code}，请检查登录和网络。")
    return response, page_status(response.text)


def validate_login(cookies):
    """添加账号时只检查会话和用户中心，不执行签到。"""
    try:
        jar, account = account_cookies(cookies)
        with new_session(jar) as session:
            response, _ = read_home(session)
            jar, refreshed_account = account_cookies(session.cookies)
        if refreshed_account != account:
            raise SafeStop("登录验证期间账号发生变化，请重新添加。")
        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")
        greeting = heading.get_text(" ", strip=True) if heading else ""
        label = greeting.removeprefix("Hi,").strip() if greeting.startswith("Hi,") else ""
        if len(label) > 80 or not label.isprintable():
            label = ""
        return jar, account, label
    except SafeStop as error:
        raise fib_users.UserError(str(error)) from None
    except requests.RequestException:
        raise fib_users.UserError("登录会话的只读验证失败，请检查网络后重新添加。") from None


def server_day(response) -> str:
    try:
        server_time = parsedate_to_datetime(response.headers["Date"])
        if server_time.tzinfo is None:
            server_time = server_time.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError):
        raise SafeStop("无法确认服务器日期，未提交签到。") from None
    now = datetime.now(timezone.utc)
    if abs((now - server_time).total_seconds()) > 300:
        raise SafeStop("本机与服务器时间相差超过 5 分钟，请先校准系统时间。")
    local_time = now.astimezone(TZ)
    # 避免零点附近预检查与提交跨天。
    if (local_time.hour == 23 and local_time.minute >= 58) or (local_time.hour == 0 and local_time.minute < 2):
        raise SafeStop("当前接近零点，请在 00:02 之后运行，避免跨天提交。")
    return server_time.astimezone(TZ).date().isoformat()


def verify_contract(session, html):
    soup = BeautifulSoup(html, "html.parser")
    if not any(script.get("src") in {SCRIPT_URL, SCRIPT_URL.removeprefix(SITE)} for script in soup.find_all("script")):
        raise SafeStop("页面引用的签到脚本已变化，需要重新检查接口。")
    response = session.get(SCRIPT_URL, timeout=TIMEOUT, allow_redirects=False)
    if response.status_code != 200 or hashlib.sha256(response.content).hexdigest() != SCRIPT_SHA256:
        raise SafeStop("网站签到代码已更新或下载异常，已停止，未提交签到。")


@contextlib.contextmanager
def account_lock(account):
    try:
        with file_lock(STATE_DIR / f"{account}.lock"):
            yield
    except BlockingIOError:
        raise SafeStop("已有另一个签到进程正在运行。") from None


def write_record(path: Path, record: dict):
    # 落盘后才允许 POST：崩溃、超时或重复运行时都不能意外再次提交。
    atomic_json(path, record)


def record_message(value) -> str:
    if not isinstance(value, str):
        return ""
    # 仅记录签到结果短文本，去掉 HTML 和控制字符。
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return "".join(c for c in text if c.isprintable())[:300]


def run(session, account: str, dry_run: bool) -> int:
    with account_lock(account):
        response, status = read_home(session)
        day = server_day(response)
        path = STATE_DIR / f"{account}-{day}.json"
        if status == "done":
            if path.exists() and not dry_run:
                record = json.loads(path.read_text(encoding="utf-8"))
                record.update(status="verified_success", verified_at=datetime.now(TZ).isoformat())
                write_record(path, record)
            print(f"{day}：网站显示今日已签到，无需提交。")
            return 0
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            print(f"{day}：今天已有提交记录（{record.get('status', 'unknown')}），不会再次提交。")
            return 0 if record.get("status") in {"verified_success", "server_confirmed"} else 3
        verify_contract(session, response.text)
        # 预检查期间若跨日，也不能使用旧日期登记。
        if datetime.now(TZ).date().isoformat() != day:
            raise SafeStop("预检查期间日期发生变化，请稍后重新运行。")
        server_day(response)  # 下载脚本后再次检查时间，避免预检查耗时进入零点保护区。
        if dry_run:
            print(f"{day}：预检查通过，登录会话有效、今日未签到、接口版本已确认；未发送签到请求。")
            return 0

        record = {"day": day, "status": "pending", "attempt_count": 1, "started_at": datetime.now(TZ).isoformat()}
        write_record(path, record)
        print("预检查通过，正在发送唯一一次签到请求……", flush=True)
        acknowledged = False
        try:
            result = session.post(
                CHECKIN_URL,
                headers={"Referer": HOME_URL, "Origin": SITE, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"},
                timeout=TIMEOUT,
                allow_redirects=False,
            )
            record["http_status"] = result.status_code
            if result.status_code == 200:
                try:
                    data = result.json()
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    record["message"] = record_message(data.get("msg"))
                    acknowledged = data.get("ret") in (1, "1", True)
            record["status"] = "server_confirmed" if acknowledged else "unconfirmed"
        except requests.RequestException as error:
            record.update(status="unconfirmed", error_type=type(error).__name__)
        write_record(path, record)

        # 仅通过 GET 复核；包括 POST 超时的情况，绝不补发 POST。
        try:
            _, final_status = read_home(session)
            if final_status == "done":
                record.update(status="verified_success", verified_at=datetime.now(TZ).isoformat())
            else:
                record.update(status="unconfirmed", verification="still_ready")
        except (SafeStop, requests.RequestException):
            record["verification"] = "unavailable"
        write_record(path, record)
        if record["status"] == "verified_success":
            print(f"签到成功，已从用户中心复核。{record.get('message', '')}")
            return 0
        if record["status"] == "server_confirmed":
            print(f"服务器已确认签到成功。{record.get('message', '')}（页面复核未确认；不会重复提交。）")
            return 0
        print(f"签到请求已提交一次，结果未确认。{record.get('message', '')} 请查看网页；今天不会再次提交。")
        return 3


def check_saved_users(selector=None, dry_run=False, on_result=None) -> int:
    users = fib_users.list_users()
    if not users:
        raise fib_users.UserError("尚未保存账号，请先执行 add user，或用 add user --from-chrome 导入当前登录账号。")
    accounts = [fib_users.resolve_user(users, selector)] if selector else list(users)
    results = []
    for number, account in enumerate(accounts, 1):
        user = users[account]
        code = 2
        print(f"[{number}/{len(accounts)}] {user['name']}：", flush=True)
        try:
            jar, actual = account_cookies(fib_users.deserialize_cookies(user["cookies"]))
            if actual != account:
                raise SafeStop("保存的会话与账号 ID 不一致，请重新添加该账号。")
            with new_session(jar) as session:
                code = run(session, account, dry_run)
                if not dry_run:
                    updated, actual = account_cookies(session.cookies)
                    if actual != account:
                        raise SafeStop("服务器返回的会话账号发生变化，未保存该会话。")
                    fib_users.refresh_user(account, updated, expected_updated_at=user.get("updated_at"))
        except (SafeStop, fib_users.UserError) as error:
            print(f"  停止：{error}", file=sys.stderr)
            code = 3 if code == 3 else 2
        except requests.RequestException as error:
            print(f"  网络读取失败（{type(error).__name__}），不会补发签到。", file=sys.stderr)
            code = 3 if code == 3 else 2
        except (OSError, ValueError):
            print("  本地账号会话或签到记录读取/写入失败，请保留原记录。", file=sys.stderr)
            code = 3 if code == 3 else 2
        results.append(code)
        if on_result is not None:
            on_result(account, code)
    label = "预检查" if dry_run else "签到"
    print(f"{label}完成：{results.count(0)}/{len(results)} 个账号正常，{results.count(2)} 个账号需处理，{results.count(3)} 个账号结果未确认。")
    return 3 if 3 in results else 2 if 2 in results else 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只检查登录和接口，不提交签到")
    parser.add_argument("--chrome-profile", help="显式使用旧版 Chrome Cookie 签到方式，例如 'Profile 2'")
    commands = parser.add_subparsers(dest="command")
    add = commands.add_parser("add", help="添加或更新账号")
    add_user = add.add_subparsers(dest="resource", required=True).add_parser("user", help="打开独立 Chrome 登录并保存账号")
    add_user.add_argument("name", nargs="?", help="账号别名，默认使用网站显示的名称或邮箱")
    add_user.add_argument("--from-chrome", action="store_true", help="导入当前 Chrome 登录态，省去再次登录")
    add_user.add_argument("--chrome-profile", default=argparse.SUPPRESS, help="导入的 Chrome 配置，默认 Profile 2")
    add_user.add_argument("--login-timeout", type=int, default=600, help="等待手动登录的秒数，默认 600")
    delete = commands.add_parser("delete", help="删除本机保存的账号会话")
    delete_user = delete.add_subparsers(dest="resource", required=True).add_parser("user")
    delete_user.add_argument("name", help="账号名称或 list users 中显示的 ID")
    listing = commands.add_parser("list", help="查看已保存的账号")
    listing.add_subparsers(dest="resource", required=True).add_parser("users")
    checkin = commands.add_parser("checkin", help="签到；默认执行全部已保存账号")
    selection = checkin.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="签到全部已保存账号（默认）")
    selection.add_argument("--user", help="只签到指定名称或 ID 的账号")
    checkin.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    checkin.add_argument("--chrome-profile", default=argparse.SUPPRESS, help="使用旧版 Chrome Cookie 签到方式")
    return parser


def main(argv=None, on_result=None, cancel_event=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"add", "delete", "list"} and args.dry_run:
        parser.error("--dry-run 只用于签到，不用于账号管理。")
    if args.command in {"delete", "list"} and args.chrome_profile:
        parser.error("此命令不使用 --chrome-profile。")
    if args.command == "checkin" and args.chrome_profile and (args.all or args.user):
        parser.error("--chrome-profile 不能与 --all 或 --user 一起使用。")
    if args.command == "add" and args.chrome_profile and not args.from_chrome:
        parser.error("--chrome-profile 仅用于 add user --from-chrome 导入。")
    try:
        if args.command == "add":
            if args.name is not None:
                args.name = fib_users.validate_name(args.name)
            if not 1 <= args.login_timeout <= 3600:
                raise fib_users.UserError("--login-timeout 须为 1～3600 秒。")
            if args.from_chrome:
                cookies, _ = chrome_cookies(args.chrome_profile or ("Default" if sys.platform == "win32" else "Profile 2"))
                cookies, account, label = validate_login(cookies)
            else:
                cookies, account, label = fib_users.capture_login(HOME_URL, validate_login, timeout=args.login_timeout, cancel_event=cancel_event)
            name, updated = fib_users.save_user(account, cookies, args.name, label)
            print(f"已{'更新' if updated else '添加'}账号：{name}（{account[:8]}）。登录会话已保存；未执行签到。")
            return 0
        if args.command == "delete":
            name = fib_users.delete_user(args.name)
            print(f"已删除本机账号：{name}。原有签到记录已保留；未修改网站账号。")
            return 0
        if args.command == "list":
            users = fib_users.list_users()
            if not users:
                print("尚未保存账号，请执行 add user。")
            for account, user in users.items():
                print(f"{user['name']}\tID: {account[:8]}\t邮箱: {user.get('email') or '未记录'}")
            return 0
        if args.chrome_profile:
            cookies, account = chrome_cookies(args.chrome_profile)
            with new_session(cookies) as session:
                return run(session, account, args.dry_run)
        return check_saved_users(getattr(args, "user", None), args.dry_run, on_result=on_result)
    except fib_users.LoginCancelled as error:
        print(str(error))
        return 130
    except (SafeStop, fib_users.UserError) as error:
        print(f"停止：{error}", file=sys.stderr)
        return 2
    except requests.RequestException as error:
        print(f"网络读取失败（{type(error).__name__}）；未自动重试签到。", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("本地账号库或签到记录读取/写入失败，已停止；请保留 .state 目录以防重复提交。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消。已有的当天提交记录会保留，不会补发签到。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    initialize_data()
    raise SystemExit(main())
