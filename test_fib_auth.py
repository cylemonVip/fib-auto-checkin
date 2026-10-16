"""自动登录离线测试；网络、密码库均使用隔离替身。"""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

import fib_auth as auth
import fib_credentials as credentials
import fib_checkin as core
import fib_users as users
from test_fib_checkin import Response, DONE, READY, JS, FixedDatetime
from test_fib_users import cookies, identity


LOGIN_SCRIPT = 'const remember_me = document.querySelector("#remember-me");'
LOGIN_HTML = '<title>登录 — FIB ACCELERATE</title><script>' + LOGIN_SCRIPT + '</script>'
PASSWORD = 'offline-password-do-not-log'
EMAIL = 'user@example.test'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.patches = [patch.object(users, 'USER_DIR', root / '.users'), patch.object(core, 'STATE_DIR', root / '.state'),
                        patch.object(auth, 'LOGIN_SCRIPT_SHA256', hashlib.sha256(LOGIN_SCRIPT.encode()).hexdigest()),
                        patch.object(core, 'datetime', FixedDatetime)]
        for item in self.patches:
            item.start()
        self.jar = cookies()
        self.account = identity(self.jar)
        users.save_user(self.account, self.jar, '测试账号')
        self.output = io.StringIO()
        self.stdout, self.stderr = redirect_stdout(self.output), redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.vault = MagicMock()
        self.vault.get_password.return_value = json.dumps({'email': EMAIL, 'password': PASSWORD})
        self.vault_patch = patch.object(credentials, 'backend', return_value=self.vault)
        self.vault_patch.start()

    def tearDown(self):
        self.assertNotIn(PASSWORD, self.output.getvalue())
        self.vault_patch.stop()
        self.stderr.__exit__(None, None, None)
        self.stdout.__exit__(None, None, None)
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def session(self, jar=None):
        session = MagicMock()
        session.__enter__.return_value = session
        session.cookies = (jar if jar is not None else self.jar).copy()
        session.get.return_value = Response(text=LOGIN_HTML)
        session.post.return_value = Response(data={'ret': 1})
        return session

    def enable(self, expire=False):
        users.configure_auto_login(self.account, True, EMAIL, self.jar)
        if expire:
            data = users._load()
            for item in data['users'][self.account]['cookies']:
                item['expires'] = 1
            with users.store_lock():
                users._write(data)

    def test_login_only_posts_to_auth_and_requests_remember_me(self):
        session = self.session()
        with patch.object(core, 'new_session', return_value=session), patch.object(core, 'validate_login', return_value=(self.jar, self.account, '')):
            result = auth.login(EMAIL, PASSWORD, self.account)
        self.assertEqual(identity(result), self.account)
        session.post.assert_called_once()
        call = session.post.call_args
        self.assertEqual(call.args[0], core.SITE + '/auth/login')
        self.assertEqual(call.kwargs['data']['remember_me'], 'on')
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertFalse(list(core.STATE_DIR.glob('*.json')))

    def test_changed_login_contract_stops_before_password_submission(self):
        session = self.session()
        session.get.return_value = Response(text=LOGIN_HTML.replace('remember_me', 'captcha'))
        with patch.object(core, 'new_session', return_value=session), self.assertRaises(users.UserError):
            auth.login(EMAIL, PASSWORD, self.account)
        session.post.assert_not_called()

    def test_two_factor_challenge_never_retries_or_validates(self):
        session = self.session()
        session.post.return_value = Response(data={'ret': 2})
        with patch.object(core, 'new_session', return_value=session), patch.object(core, 'validate_login') as validate, self.assertRaisesRegex(users.UserError, '动态令牌'):
            auth.login(EMAIL, PASSWORD, self.account)
        self.assertEqual(session.post.call_count, 1)
        validate.assert_not_called()

    def test_wrong_password_message_cannot_leak_server_echo(self):
        session = self.session()
        session.post.return_value = Response(data={'ret': 0, 'msg': PASSWORD})
        with patch.object(core, 'new_session', return_value=session), self.assertRaises(users.UserError) as error:
            auth.login(EMAIL, PASSWORD, self.account)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_wrong_account_never_stores_password_or_cookies(self):
        session = self.session(cookies('200'))
        with patch.object(core, 'new_session', return_value=session), self.assertRaisesRegex(users.UserError, '另一个'):
            auth.configure(self.account, EMAIL, PASSWORD)
        self.vault.set_password.assert_not_called()
        self.assertEqual(identity(users.deserialize_cookies(users.list_users()[self.account]['cookies'])), self.account)

    def test_timeout_never_retries_login(self):
        session = self.session()
        session.post.side_effect = requests.ReadTimeout(PASSWORD)
        with patch.object(core, 'new_session', return_value=session), self.assertRaises(users.UserError) as error:
            auth.login(EMAIL, PASSWORD, self.account)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_configure_validates_before_saving_only_in_system_vault(self):
        with patch.object(auth, 'login', return_value=self.jar) as login:
            auth.configure(self.account, EMAIL, PASSWORD)
        login.assert_called_once_with(EMAIL, PASSWORD, self.account)
        self.vault.set_password.assert_called_once()
        saved = users.list_users()[self.account]
        self.assertTrue(saved['auto_login'])
        self.assertEqual(saved['login_email'], EMAIL)
        self.assertNotIn(PASSWORD, (users.USER_DIR / 'users.json').read_text(encoding='utf-8'))
        self.assertFalse(list(core.STATE_DIR.glob('*.json')))

    def test_failed_configure_never_enables_auto_login(self):
        with patch.object(auth, 'login', side_effect=users.UserError('登录失败')), self.assertRaises(users.UserError):
            auth.configure(self.account, EMAIL, PASSWORD)
        self.vault.set_password.assert_not_called()
        self.assertFalse(users.list_users()[self.account]['auto_login'])

    def test_vault_failure_is_sanitized_and_cannot_fall_back_to_file(self):
        self.vault.set_password.side_effect = RuntimeError(PASSWORD)
        with patch.object(auth, 'login', return_value=self.jar), self.assertRaises(users.UserError) as error:
            auth.configure(self.account, EMAIL, PASSWORD)
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertFalse(users.list_users()[self.account]['auto_login'])
        self.assertNotIn(PASSWORD, (users.USER_DIR / 'users.json').read_text(encoding='utf-8'))

    def test_disable_removes_system_password_and_keeps_checkin_history(self):
        self.enable()
        core.STATE_DIR.mkdir(exist_ok=True)
        path = core.STATE_DIR / f'{self.account}-2026-10-16.json'
        path.write_text('{"status":"pending"}', encoding='utf-8')
        auth.disable(self.account)
        self.vault.delete_password.assert_called_once()
        self.assertFalse(users.list_users()[self.account]['auto_login'])
        self.assertTrue(path.exists())

    def test_delete_user_also_removes_system_password(self):
        self.enable()
        users.delete_user('测试账号')
        self.vault.delete_password.assert_called_once()
        self.assertEqual(users.list_users(), {})

    def test_delete_cannot_race_with_active_account_operation(self):
        self.enable()
        with core.account_lock(self.account), self.assertRaises(users.UserBusy):
            users.delete_user('测试账号')
        self.assertIn(self.account, users.list_users())
        self.vault.delete_password.assert_not_called()

    def test_manual_relogin_preserves_auto_login_configuration(self):
        self.enable()
        users.save_user(self.account, self.jar, '新名称')
        self.assertTrue(users.list_users()[self.account]['auto_login'])
        self.assertEqual(users.list_users()[self.account]['login_email'], EMAIL)

    def test_failed_restore_reserves_cooldown_before_network_and_survives_restart(self):
        self.enable(expire=True)
        path = users.USER_DIR / 'auth' / f'{self.account}.json'
        def fail(*args):
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'pending')
            raise users.UserError('登录失败')
        with patch.object(auth, 'login', side_effect=fail) as login:
            with self.assertRaises(users.UserError):
                auth.restore(self.account)
            with self.assertRaisesRegex(users.UserError, '30 分钟'):
                auth.restore(self.account)
            self.assertEqual(login.call_count, 1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'failed')
        self.assertFalse(list(core.STATE_DIR.glob('*.json')))

    def test_expired_cookie_recovers_once_before_checkin(self):
        self.enable(expire=True)
        session = self.session()
        with patch.object(auth, 'login', return_value=self.jar) as login, patch.object(core, 'new_session', return_value=session), patch.object(core, 'run', return_value=0) as run:
            self.assertEqual(core.check_saved_users(), 0)
        login.assert_called_once()
        run.assert_called_once_with(session, self.account, False)

    def test_server_login_redirect_recovers_once_before_checkin(self):
        self.enable()
        sessions = [self.session(), self.session()]
        with patch.object(core, 'new_session', side_effect=sessions), patch.object(core, 'run', side_effect=[core.AuthExpired('过期'), 0]) as run, patch.object(auth, 'login', return_value=self.jar) as login:
            self.assertEqual(core.check_saved_users(), 0)
        self.assertEqual(run.call_count, 2)
        login.assert_called_once()

    def test_dry_run_never_auto_logs_in_or_changes_credentials(self):
        self.enable(expire=True)
        with patch.object(auth, 'restore') as restore, patch.object(core, 'new_session') as session:
            self.assertEqual(core.check_saved_users(dry_run=True), 2)
        restore.assert_not_called()
        session.assert_not_called()
        self.vault.get_password.assert_not_called()

    def test_locked_system_vault_stops_before_login_or_checkin(self):
        self.enable(expire=True)
        self.vault.get_password.side_effect = RuntimeError(PASSWORD)
        with patch.object(auth, 'login') as login, patch.object(core, 'run') as run:
            self.assertEqual(core.check_saved_users(), 2)
        login.assert_not_called()
        run.assert_not_called()

    def test_network_or_contract_error_does_not_trigger_auto_login(self):
        self.enable()
        session = self.session()
        for error in (requests.ConnectionError(), core.SafeStop('页面结构变化')):
            with self.subTest(error=type(error).__name__), patch.object(core, 'new_session', return_value=session), patch.object(core, 'run', side_effect=error), patch.object(auth, 'restore') as restore:
                self.assertEqual(core.check_saved_users(), 2)
                restore.assert_not_called()

    def test_post_submission_cookie_failure_cannot_trigger_auto_login(self):
        self.enable()
        session = self.session()
        def run(*args):
            session.cookies.clear()
            return 3
        with patch.object(core, 'new_session', return_value=session), patch.object(core, 'run', side_effect=run), patch.object(auth, 'restore') as restore:
            self.assertEqual(core.check_saved_users(), 3)
        restore.assert_not_called()

    def test_persisted_daily_attempt_is_kept_after_restoring_login(self):
        self.enable(expire=True)
        core.STATE_DIR.mkdir(exist_ok=True)
        path = core.STATE_DIR / f'{self.account}-2026-10-16.json'
        path.write_text('{"status":"pending","attempt_count":1}', encoding='utf-8')
        session = self.session()
        session.get.return_value = Response(text=READY)
        with patch.object(auth, 'login', return_value=self.jar), patch.object(core, 'new_session', return_value=session):
            self.assertEqual(core.check_saved_users(), 3)
        session.post.assert_not_called()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['attempt_count'], 1)

    def test_restored_session_still_submits_checkin_at_most_once(self):
        self.enable(expire=True)
        session = self.session()
        posts = []
        def get(url, **kwargs):
            return Response(content=JS) if url == core.SCRIPT_URL else Response(text=DONE if posts else READY)
        def post(url, **kwargs):
            self.assertEqual(url, core.CHECKIN_URL)
            posts.append(url)
            return Response(data={'ret': 1})
        session.get.side_effect = get
        session.post.side_effect = post
        with patch.object(auth, 'login', return_value=self.jar), patch.object(core, 'new_session', return_value=session), patch.object(core, 'SCRIPT_SHA256', hashlib.sha256(JS).hexdigest()):
            self.assertEqual(core.check_saved_users(), 0)
            self.assertEqual(core.check_saved_users(), 0)
        self.assertEqual(posts, [core.CHECKIN_URL])

    def test_only_confirmed_login_redirect_or_login_page_means_expired(self):
        session = self.session()
        response = Response(status=302)
        response.headers['Location'] = '/auth/login'
        session.get.return_value = response
        with self.assertRaises(core.AuthExpired):
            core.read_home(session)
        response.headers['Location'] = 'https://example.test/auth/login'
        with self.assertRaises(core.SafeStop) as error:
            core.read_home(session)
        self.assertNotIsInstance(error.exception, core.AuthExpired)
        session.get.return_value = Response(text='<title>登录 — FIB ACCELERATE</title>')
        with self.assertRaises(core.AuthExpired):
            core.read_home(session)

    def test_credential_namespace_isolated_by_data_directory(self):
        before = credentials.service()
        with patch.object(users, 'USER_DIR', Path(self.folder.name) / 'different'):
            self.assertNotEqual(before, credentials.service())

    def test_cli_configure_password_is_prompted_never_an_argument(self):
        with patch('getpass.getpass', return_value=PASSWORD), patch.object(auth, 'configure') as configure:
            self.assertEqual(core.main(['autologin', 'user', '测试账号', '--email', EMAIL]), 0)
        configure.assert_called_once_with(self.account, EMAIL, PASSWORD)


if __name__ == '__main__':
    unittest.main()
