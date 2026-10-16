"""账号管理与批量流程离线测试：不使用真实登录，不发送真实签到请求。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

import fib_checkin as fib
import fib_users as users
from test_fib_checkin import DONE, JS, READY, Response, FixedDatetime


def cookies(uid="100", key="test-session"):
    jar = requests.cookies.RequestsCookieJar()
    jar.set("uid", uid, domain=fib.HOST, path="/", secure=True)
    jar.set("key", key, domain=fib.HOST, path="/", secure=True)
    return jar


def identity(jar):
    return fib.account_cookies(jar)[1]


class UserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.user_patch = patch.object(users, "USER_DIR", root / "users")
        self.state_patch = patch.object(fib, "STATE_DIR", root / "state")
        self.clock_patch = patch.object(fib, "datetime", FixedDatetime)
        self.user_patch.start()
        self.state_patch.start()
        self.clock_patch.start()
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stderr = redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        self.stdout.__exit__(None, None, None)
        self.user_patch.stop()
        self.state_patch.stop()
        self.clock_patch.stop()
        self.folder.cleanup()

    def add(self, uid="100", name="主账号"):
        jar = cookies(uid)
        account = identity(jar)
        users.save_user(account, jar, name, "user@example.test")
        return account

    def session(self, jar):
        session = MagicMock()
        session.__enter__.return_value = session
        session.cookies = jar.copy()
        return session

    def test_save_round_trip_with_private_permissions(self):
        account = self.add()
        saved = users.list_users()[account]
        self.assertEqual(identity(users.deserialize_cookies(saved["cookies"])), account)
        if os.name != "nt":
            self.assertEqual(os.stat(users.USER_DIR).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(users.USER_DIR / "users.json").st_mode & 0o777, 0o600)
        self.assertFalse((fib.STATE_DIR).exists())

    def test_same_account_updates_instead_of_duplicating(self):
        account = self.add()
        name, updated = users.save_user(account, cookies(key="new-session"))
        self.assertTrue(updated)
        self.assertEqual(name, "主账号")
        self.assertEqual(len(users.list_users()), 1)
        self.assertEqual(users.deserialize_cookies(users.list_users()[account]["cookies"]).get("key"), "new-session")

    def test_name_collision_never_overwrites_another_account(self):
        self.add()
        with self.assertRaises(users.UserError):
            self.add("200", "主账号")
        self.assertEqual(len(users.list_users()), 1)

    def test_delete_keeps_attempt_history_and_readding_identity(self):
        account = self.add()
        fib.STATE_DIR.mkdir()
        history = fib.STATE_DIR / f"{account}-2026-10-16.json"
        history.write_text('{"status":"pending","attempt_count":1}')
        self.assertEqual(users.delete_user("主账号"), "主账号")
        self.assertEqual(users.list_users(), {})
        self.assertTrue(history.exists())
        self.assertEqual(self.add(name="重新添加"), account)
        self.assertTrue(history.exists())

    def test_delete_unknown_user_has_no_effect(self):
        self.add()
        with self.assertRaises(users.UserError):
            users.delete_user("不存在")
        self.assertEqual(len(users.list_users()), 1)

    def test_refresh_cannot_resurrect_deleted_user(self):
        account = self.add()
        users.delete_user("主账号")
        users.refresh_user(account, cookies())
        self.assertEqual(users.list_users(), {})

    def test_refresh_cannot_overwrite_newer_login(self):
        account = self.add()
        old_time = users.list_users()[account]["updated_at"]
        users.save_user(account, cookies(key="fresh-login"))
        users.refresh_user(account, cookies(key="stale-session"), expected_updated_at=old_time)
        self.assertEqual(users.deserialize_cookies(users.list_users()[account]["cookies"]).get("key"), "fresh-login")

    def test_website_handle_is_used_as_name_without_fake_email(self):
        jar = cookies()
        account = identity(jar)
        name, _ = users.save_user(account, jar, label="sean")
        self.assertEqual(name, "sean")
        self.assertEqual(users.list_users()[account]["email"], "")

    def test_broken_database_is_not_overwritten(self):
        self.add()
        path = users.USER_DIR / "users.json"
        path.write_text("broken")
        with self.assertRaises(users.UserError):
            self.add("200", "副账号")
        self.assertEqual(path.read_text(), "broken")

    def test_cookie_filter_excludes_other_sites_and_expired_sessions(self):
        jar = cookies()
        jar.set("key", "unrelated", domain="example.test")
        filtered, _ = fib.account_cookies(jar)
        self.assertEqual({c.domain for c in filtered}, {fib.HOST})
        items = users.serialize_cookies(cookies())
        for item in items:
            item["expires"] = 1
        with self.assertRaises(fib.SafeStop):
            fib.account_cookies(users.deserialize_cookies(items))

    def test_session_cookies_survive_save_and_reload(self):
        items = users.serialize_cookies(cookies())
        for item in items:
            item["expires"] = -1
        jar = users.deserialize_cookies(items)
        self.assertTrue(all(c.expires is None for c in jar))
        self.assertEqual(identity(jar), identity(cookies()))

    def test_cli_add_from_chrome_only_validates_get(self):
        jar = cookies()
        session = self.session(jar)
        session.get.return_value = Response(text=DONE.replace("</title>", "</title><h1>Hi, user@example.test</h1>"))
        with patch.object(fib, "chrome_cookies", return_value=(jar, identity(jar))), patch.object(fib, "new_session", return_value=session):
            self.assertEqual(fib.main(["add", "user", "主账号", "--from-chrome"]), 0)
        session.post.assert_not_called()
        self.assertEqual(len(users.list_users()), 1)
        self.assertNotIn("test-session", self.output.getvalue())

    def test_cli_add_opens_login_and_saves_without_checkin(self):
        jar = cookies()
        with patch.object(users, "capture_login", return_value=(jar, identity(jar), "user@example.test")) as capture, patch.object(fib, "run") as run:
            self.assertEqual(fib.main(["add", "user", "新账号"]), 0)
            capture.assert_called_once()
            run.assert_not_called()
        self.assertEqual(users.list_users()[identity(jar)]["name"], "新账号")

    def test_cancelled_login_is_reported_as_cancelled_and_saves_nothing(self):
        with patch.object(users, "capture_login", side_effect=users.LoginCancelled("已取消登录")):
            self.assertEqual(fib.main(["add", "user", "取消测试"]), 130)
        self.assertEqual(users.list_users(), {})

    def test_cli_list_and_delete_do_not_touch_network(self):
        self.add()
        with patch.object(fib, "new_session") as session:
            self.assertEqual(fib.main(["list", "users"]), 0)
            self.assertEqual(fib.main(["delete", "user", "主账号"]), 0)
            session.assert_not_called()
        self.assertNotIn("test-session", self.output.getvalue())

    def test_no_saved_accounts_does_not_fall_back_to_deleted_chrome_user(self):
        with patch.object(fib, "chrome_cookies") as chrome:
            self.assertEqual(fib.main([]), 2)
            chrome.assert_not_called()

    def test_batch_continues_after_expired_account(self):
        expired = self.add()
        valid = self.add("200", "副账号")
        data = users._load()
        for item in data["users"][expired]["cookies"]:
            item["expires"] = 1
        with users.store_lock():
            users._write(data)
        session = self.session(cookies("200"))
        with patch.object(fib, "new_session", return_value=session), patch.object(fib, "run", return_value=0) as run:
            self.assertEqual(fib.check_saved_users(), 2)
            run.assert_called_once_with(session, valid, False)

    def test_selected_account_only_and_dry_run_never_refreshes_storage(self):
        self.add()
        selected = self.add("200", "副账号")
        session = self.session(cookies("200"))
        with patch.object(fib, "new_session", return_value=session), patch.object(fib, "run", return_value=0) as run, patch.object(users, "refresh_user") as refresh:
            self.assertEqual(fib.main(["checkin", "--user", "副账号", "--dry-run"]), 0)
            run.assert_called_once_with(session, selected, True)
            refresh.assert_not_called()

    def test_two_accounts_each_submit_once_even_after_batch_repeated(self):
        self.add()
        self.add("200", "副账号")
        sessions = {}

        class BatchSession:
            def __init__(self, jar):
                self.cookies = jar.copy()
                self.account = identity(jar)
                self.posts = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, **kwargs):
                assert kwargs["allow_redirects"] is False
                if url == fib.SCRIPT_URL:
                    return Response(content=JS)
                assert url == fib.HOME_URL
                return Response(text=DONE if self.posts else READY)

            def post(self, url, **kwargs):
                assert url == fib.CHECKIN_URL
                assert kwargs["allow_redirects"] is False
                records = list(fib.STATE_DIR.glob(f"{self.account}-*.json"))
                assert len(records) == 1 and json.loads(records[0].read_text())["status"] == "pending"
                self.posts += 1
                return Response(data={"ret": 1, "msg": "离线签到成功"})

        def session_for(jar):
            account = identity(jar)
            if account not in sessions:
                sessions[account] = BatchSession(jar)
            return sessions[account]

        with patch.object(fib, "new_session", side_effect=session_for), patch.object(fib, "SCRIPT_SHA256", hashlib.sha256(JS).hexdigest()):
            self.assertEqual(fib.check_saved_users(), 0)
            self.assertEqual(fib.check_saved_users(), 0)
        self.assertEqual([session.posts for session in sessions.values()], [1, 1])
        records = [json.loads(p.read_text()) for p in fib.STATE_DIR.glob("*.json")]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["attempt_count"] == 1 and r["status"] == "verified_success" for r in records))

    def test_saved_identity_mismatch_blocks_checkin(self):
        account = self.add()
        data = users._load()
        data["users"][account]["cookies"] = users.serialize_cookies(cookies("200"))
        with users.store_lock():
            users._write(data)
        with patch.object(fib, "run") as run:
            self.assertEqual(fib.check_saved_users(), 2)
            run.assert_not_called()

    def test_explicit_chrome_option_preserves_legacy_usage(self):
        jar = cookies()
        session = self.session(jar)
        with patch.object(fib, "chrome_cookies", return_value=(jar, identity(jar))) as chrome, patch.object(fib, "new_session", return_value=session), patch.object(fib, "run", return_value=0):
            self.assertEqual(fib.main(["--chrome-profile", "Profile 2", "--dry-run"]), 0)
            chrome.assert_called_once_with("Profile 2")


class LoginWindowTests(unittest.TestCase):
    def fake_browser(self):
        from playwright import sync_api
        runtime = MagicMock()
        playwright = runtime.__enter__.return_value
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        context.pages = [page]
        page.is_closed.return_value = False
        browser.is_connected.return_value = True
        context.cookies.return_value = users.serialize_cookies(cookies())
        return runtime, playwright, browser, context, page

    def test_login_window_is_separate_and_blocks_checkin_requests(self):
        runtime, playwright, browser, context, _ = self.fake_browser()
        with patch("playwright.sync_api.sync_playwright", return_value=runtime), redirect_stdout(io.StringIO()):
            result = users.capture_login(fib.HOME_URL, lambda jar: (jar, identity(jar), "user@example.test"), timeout=1)
        playwright.chromium.launch.assert_called_once_with(channel="chrome", headless=False, chromium_sandbox=True, timeout=30000)
        guard = context.route.call_args.args[1]
        route = MagicMock()
        route.request.url = fib.CHECKIN_URL + "?accidental-click=1"
        guard(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        login_route = MagicMock()
        login_route.request.url = fib.SITE + "/auth/login"
        guard(login_route)
        login_route.continue_.assert_called_once()
        browser.close.assert_called_once()
        self.assertEqual(result[1], identity(cookies()))

    def test_closed_window_saves_nothing_and_closes_process(self):
        runtime, _, browser, context, _ = self.fake_browser()
        context.pages = []
        validate = MagicMock()
        with patch("playwright.sync_api.sync_playwright", return_value=runtime), redirect_stdout(io.StringIO()), self.assertRaises(users.UserError):
            users.capture_login(fib.HOME_URL, validate, timeout=1)
        validate.assert_not_called()
        browser.close.assert_called_once()

    def test_login_timeout_never_calls_validator_without_auth_cookies(self):
        runtime, _, browser, context, _ = self.fake_browser()
        context.cookies.return_value = []
        validate = MagicMock()
        with patch("playwright.sync_api.sync_playwright", return_value=runtime), patch.object(users.time, "monotonic", side_effect=[0, 2]), redirect_stdout(io.StringIO()), self.assertRaises(users.UserError):
            users.capture_login(fib.HOME_URL, validate, timeout=1)
        validate.assert_not_called()
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
