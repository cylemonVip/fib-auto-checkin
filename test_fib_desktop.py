"""桌面、调度和迁移离线验证；不发起任何真实签到。"""

import contextlib
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTime
import fib_checkin as core
import fib_users as users
import fib_storage as storage
from fib_desktop_model import DesktopStore, TZ
from fib_desktop import MainWindow
from test_fib_users import cookies, identity


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = DesktopStore(self.folder.name)
        self.morning = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)

    def tearDown(self):
        self.folder.cleanup()

    def enable(self, **extra):
        self.store.save_settings({"enabled": True, "time": "09:00", **extra})

    def test_schedule_defaults_off(self):
        self.assertFalse(self.store.claim_due(self.morning))
        self.assertIsNone(self.store.next_run(self.morning))

    def test_due_claim_survives_restart_and_runs_once_daily(self):
        self.enable()
        self.assertFalse(self.store.claim_due(self.morning - timedelta(minutes=1)))
        self.assertTrue(self.store.claim_due(self.morning))
        restarted = DesktopStore(self.folder.name)
        self.assertFalse(restarted.claim_due(self.morning + timedelta(hours=1)))
        self.assertTrue(restarted.claim_due(self.morning + timedelta(days=1)))

    def test_busy_or_empty_accounts_does_not_consume_daily_schedule(self):
        self.enable()
        self.assertFalse(self.store.claim_due(self.morning, busy=True))
        self.assertFalse(self.store.claim_due(self.morning, has_users=False))
        self.assertTrue(self.store.claim_due(self.morning))

    def test_catchup_after_sleep(self):
        self.enable(catch_up=True)
        self.assertTrue(self.store.claim_due(self.morning + timedelta(hours=4)))

    def test_no_catchup_does_not_run_missed_plan(self):
        self.enable(catch_up=False)
        self.assertFalse(self.store.claim_due(self.morning + timedelta(minutes=1)))
        self.assertTrue(self.store.claim_due(self.morning + timedelta(days=1)))

    def test_clock_rollback_cannot_repeat_claim(self):
        self.enable()
        self.assertTrue(self.store.claim_due(self.morning))
        self.assertFalse(self.store.claim_due(self.morning - timedelta(days=1)))

    def test_time_change_does_not_reset_today_claim(self):
        self.enable()
        self.assertTrue(self.store.claim_due(self.morning))
        self.store.save_settings({"time": "10:00"})
        self.assertFalse(self.store.claim_due(self.morning + timedelta(hours=1)))

    def test_midnight_boundary_never_runs(self):
        self.enable()
        self.assertFalse(self.store.claim_due(self.morning.replace(hour=23, minute=59)))
        for value in ("00:00", "00:01", "23:58", "23:59", "25:00"):
            with self.assertRaises(ValueError):
                self.store.save_settings({"time": value})

    def test_next_run_moves_to_tomorrow_after_claim(self):
        self.enable()
        self.assertEqual(self.store.next_run(self.morning - timedelta(hours=1)), self.morning)
        self.assertTrue(self.store.claim_due(self.morning))
        self.assertEqual(self.store.next_run(self.morning), self.morning + timedelta(days=1))

    def test_precheck_result_is_not_marked_as_checkin(self):
        self.store.record_result("test-account", 0, dry_run=True)
        self.assertNotIn("checkin", self.store.results()["test-account"])
        self.store.record_result("test-account", 3)
        self.assertEqual(self.store.results()["test-account"]["checkin"]["code"], 3)

    def test_corrupted_config_fails_closed(self):
        self.store.root.mkdir(exist_ok=True)
        self.store.settings_path.write_text("invalid")
        with self.assertRaises(ValueError):
            self.store.claim_due(self.morning)


class StorageTests(unittest.TestCase):
    def test_legacy_migration_preserves_records_and_does_not_resurrect_users(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy, target = Path(folder) / "legacy", Path(folder) / "target"
            storage.atomic_json(legacy / ".state/account-2026-10-16.json", {"status": "pending", "attempt_count": 1})
            storage.atomic_json(legacy / ".users/users.json", {"version": 1, "users": {"test": {}}})
            with patch.object(storage, "data_root", return_value=target), patch.dict(os.environ, {"FIB_DATA_DIR": ""}):
                storage.initialize_data(legacy)
                self.assertEqual(json.loads((target / ".state/account-2026-10-16.json").read_text())["attempt_count"], 1)
                self.assertTrue((legacy / ".users/users.json").exists())
                storage.atomic_json(target / ".users/users.json", {"version": 1, "users": {}})
                storage.initialize_data(legacy)
                self.assertEqual(json.loads((target / ".users/users.json").read_text())["users"], {})

    def test_explicit_test_data_directory_never_imports_real_accounts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"FIB_DATA_DIR": folder}):
            storage.initialize_data(Path(__file__).resolve().parent)
            self.assertFalse((Path(folder) / ".users/users.json").exists())

    def test_lock_is_exclusive_across_file_handles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "exclusive.lock"
            with storage.file_lock(path):
                with self.assertRaises(BlockingIOError):
                    with storage.file_lock(path):
                        self.fail("second handle acquired lock")
            with storage.file_lock(path):
                pass

    def test_windows_data_path_uses_localappdata(self):
        with patch.object(storage.sys, "platform", "win32"), patch.dict(os.environ, {"FIB_DATA_DIR": "", "LOCALAPPDATA": "/tmp/test-localappdata"}):
            self.assertEqual(storage.data_root(), Path("/tmp/test-localappdata/FIB Checkin"))


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.users_patch = patch.object(users, "USER_DIR", root / ".users")
        self.state_patch = patch.object(core, "STATE_DIR", root / ".state")
        self.users_patch.start()
        self.state_patch.start()
        jar = cookies()
        self.account = identity(jar)
        users.save_user(self.account, jar, "测试账号")
        self.store = DesktopStore(root)
        self.window = MainWindow(self.store, tray_enabled=False)
        self.window.timer.stop()

    def tearDown(self):
        self.assertIsNone(self.window.worker)
        self.window.has_tray = False
        self.window.close()
        self.app.processEvents()
        for handler in self.window.logger.handlers:
            handler.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.users_patch.stop()
        self.state_patch.stop()
        self.folder.cleanup()

    def wait_worker(self):
        deadline = time.monotonic() + 5
        while self.window.worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(self.window.worker)

    def test_accounts_render_without_network(self):
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertIn("测试账号", self.window.table.item(0, 0).text())
        self.assertEqual(self.window.metrics[1].text(), "0")

    def test_accessible_navigation_changes_page_on_checked_state(self):
        self.window.nav_group.button(2).setChecked(True)
        self.assertEqual(self.window.pages.currentIndex(), 2)
        self.assertEqual(self.window.page_title.text(), "运行日志")
        self.window.nav_group.button(1).setChecked(True)
        self.assertEqual(self.window.pages.currentIndex(), 1)

    def test_schedule_settings_persist(self):
        self.window.enabled.setChecked(True)
        self.window.daily_time.setTime(QTime(10, 35))
        self.window.save_schedule()
        self.assertEqual(self.store.settings()["time"], "10:35")
        self.assertTrue(self.store.settings()["enabled"])

    def test_close_hides_window_and_keeps_timer_running(self):
        self.window.has_tray = True
        self.window.show()
        self.window.timer.start()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertTrue(self.window.timer.isActive())
        self.window.timer.stop()

    def test_background_task_precheck_reports_result_without_checkin(self):
        def fake_main(argv, on_result, cancel_event):
            self.assertIn("--dry-run", argv)
            print("离线预检查通过；未发送签到请求。")
            on_result(self.account, 0)
            return 0
        with patch.object(core, "main", side_effect=fake_main), patch.object(core, "new_session", side_effect=AssertionError("unexpected network")):
            self.assertTrue(self.window.start_task(["checkin", "--all", "--dry-run"], "测试预检查"))
            self.assertFalse(self.window.start_task(["checkin", "--all"], "不允许并发"))
            self.wait_worker()
        self.assertIn("离线预检查通过", self.window.logs.toPlainText())
        self.assertNotIn("checkin", self.store.results()[self.account])

    def test_scheduled_tick_dispatches_only_one_batch(self):
        self.store.save_settings({"enabled": True, "time": "09:00"})
        fixed_time = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
        with patch.object(core, "main", return_value=0) as main, patch.object(self.store, "claim_due", side_effect=lambda **kwargs: DesktopStore.claim_due(self.store, now=fixed_time, **kwargs)):
            self.window.tick()
            self.wait_worker()
            self.window.tick()
            self.wait_worker()
            self.assertEqual(main.call_count, 1)


if __name__ == "__main__":
    unittest.main()
