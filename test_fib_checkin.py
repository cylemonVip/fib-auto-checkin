"""离线验证：不访问 FIB、不读取真实 Cookie、不消耗签到机会。"""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import fib_checkin as fib


JS = b"offline contract fixture"
FIXED_TIME = datetime(2026, 10, 16, 1, 0, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_TIME.astimezone(tz) if tz is not None else FIXED_TIME.replace(tzinfo=None)


READY = f'''<html><title>用户中心 — FIB ACCELERATE</title>
<div class="card-header"><h3>节点订阅链接</h3>
<button id="checkin"><i></i>&nbsp;每日签到</button></div>
<script src="{fib.SCRIPT_URL}"></script></html>'''
DONE = READY.replace('<button id="checkin"><i></i>&nbsp;每日签到</button>', '<span>今日已签到</span>')


class Response:
    def __init__(self, text="", content=b"", data=None, status=200):
        self.text, self.content, self.data, self.status_code = text, content, data, status
        self.headers = {"Date": format_datetime(FIXED_TIME, usegmt=True)}

    def json(self):
        if self.data is None:
            raise ValueError("not JSON")
        return self.data


class Session:
    def __init__(self, before=READY, after=DONE, result=None):
        self.before, self.after = before, after
        self.result = result or Response(data={"ret": 1, "msg": "获得 100MB 流量"})
        self.posts, self.home_reads = 0, 0
        self.contract = JS

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        if url == fib.SCRIPT_URL:
            return Response(content=self.contract)
        assert url == fib.HOME_URL
        self.home_reads += 1
        return Response(text=self.before if self.home_reads == 1 else self.after)

    def post(self, url, **kwargs):
        assert url == fib.CHECKIN_URL
        assert kwargs["allow_redirects"] is False
        assert not kwargs.get("data") and not kwargs.get("json")
        pending = list(fib.STATE_DIR.glob("*.json"))
        assert len(pending) == 1
        assert json.loads(pending[0].read_text(encoding="utf-8"))["status"] == "pending"
        self.posts += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class CheckinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = patch.object(fib, "STATE_DIR", Path(self.temp.name))
        self.digest = patch.object(fib, "SCRIPT_SHA256", hashlib.sha256(JS).hexdigest())
        self.clock = patch.object(fib, "datetime", FixedDatetime)
        self.state.start()
        self.digest.start()
        self.clock.start()
        self.output = io.StringIO()
        self.stdout = redirect_stdout(self.output)
        self.stdout.__enter__()

    def tearDown(self):
        self.stdout.__exit__(None, None, None)
        self.digest.stop()
        self.clock.stop()
        self.state.stop()
        self.temp.cleanup()

    def records(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in fib.STATE_DIR.glob("*.json")]

    def test_dry_run_never_posts_or_reserves_attempt(self):
        session = Session()
        self.assertEqual(fib.run(session, "test-account", True), 0)
        self.assertEqual(session.posts, 0)
        self.assertEqual(self.records(), [])

    def test_success_requires_one_post_and_page_verification(self):
        session = Session()
        self.assertEqual(fib.run(session, "test-account", False), 0)
        self.assertEqual(session.posts, 1)
        self.assertEqual(self.records()[0]["status"], "verified_success")

    def test_already_done_never_posts(self):
        session = Session(before=DONE)
        self.assertEqual(fib.run(session, "test-account", False), 0)
        self.assertEqual(session.posts, 0)

    def test_timeout_then_success_uses_get_only(self):
        session = Session(result=requests.Timeout())
        self.assertEqual(fib.run(session, "test-account", False), 0)
        self.assertEqual(session.posts, 1)
        self.assertEqual(self.records()[0]["status"], "verified_success")

    def test_timeout_and_second_run_do_not_retry(self):
        first = Session(result=requests.Timeout(), after=READY)
        self.assertEqual(fib.run(first, "test-account", False), 3)
        second = Session()
        self.assertEqual(fib.run(second, "test-account", False), 3)
        self.assertEqual((first.posts, second.posts), (1, 0))
        self.assertEqual(self.records()[0]["attempt_count"], 1)

    def test_changed_website_code_stops_before_post(self):
        session = Session()
        session.contract = b"changed"
        with self.assertRaises(fib.SafeStop):
            fib.run(session, "test-account", False)
        self.assertEqual(session.posts, 0)
        self.assertEqual(self.records(), [])

    def test_logged_out_stops_before_post(self):
        session = Session(before="<title>登录 — FIB ACCELERATE</title>")
        with self.assertRaises(fib.SafeStop):
            fib.run(session, "test-account", False)
        self.assertEqual(session.posts, 0)

    def test_contradictory_page_is_not_reported_as_success(self):
        session = Session(after=READY)
        self.assertEqual(fib.run(session, "test-account", False), 3)
        self.assertEqual(session.posts, 1)
        self.assertEqual(self.records()[0]["status"], "unconfirmed")

    def test_redirect_or_invalid_json_is_not_retried(self):
        for result in [Response(status=302), Response(text="not JSON")]:
            account = f"account-{result.status_code}"
            session = Session(result=result, after=READY)
            # 每个场景使用独立的状态目录。
            with tempfile.TemporaryDirectory() as folder, patch.object(fib, "STATE_DIR", Path(folder)):
                self.assertEqual(fib.run(session, account, False), 3)
                self.assertEqual(session.posts, 1)

    def test_concurrent_process_is_blocked(self):
        with fib.account_lock("test-account"):
            with self.assertRaises(fib.SafeStop):
                with fib.account_lock("test-account"):
                    self.fail("second lock was allowed")

    def test_fresh_session_has_no_transport_retries(self):
        with fib.new_session(requests.cookies.RequestsCookieJar()) as session:
            self.assertEqual(session.get_adapter(fib.SITE).max_retries.total, 0)


if __name__ == "__main__":
    unittest.main()
