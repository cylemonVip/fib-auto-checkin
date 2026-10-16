#!/usr/bin/env python3
"""FIB Checkin：macOS / Windows 桌面和托盘应用。"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
import hashlib
import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import os

from PySide6.QtCore import QLockFile, QSize, Qt, QTime, QTimer, Signal, QThread
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QIcon, QKeySequence, QFontDatabase, QRawFont
from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTimeEdit, QVBoxLayout, QWidget,
)

import fib_checkin as core
import fib_users as users
from fib_desktop_model import DesktopStore, TZ, account_rows
from fib_storage import initialize_data, private_dir, atomic_json


ASSETS = Path(__file__).resolve().parent / "assets"


def configure_font(app):
    font_id = QFontDatabase.addApplicationFont(str(ASSETS / "fonts/NotoSansSC.ttf"))
    families = QFontDatabase.applicationFontFamilies(font_id)
    if font_id < 0 or not families:
        raise ValueError("内置字体加载失败，请重新解压完整的应用包。")
    font = QFont(families[0], 10)
    font.setWeight(QFont.Weight.Normal)
    app.setFont(font)
    return families[0]


STYLE = """
QWidget { color: #183442; font-size: 13px; }
QMainWindow, QWidget#content { background: #F3F6F8; }
QWidget#sidebar { background: #112D3D; }
QLabel#brand { color: #EDF8F5; font-size: 27px; font-weight: 700; }
QLabel#sidebarNote { color: #A9C3CB; font-size: 12px; }
QPushButton#nav { background: transparent; color: #B8CED6; border: none; text-align: left; padding: 13px 18px; border-radius: 9px; }
QPushButton#nav:checked { background: #244654; color: #FFFFFF; font-weight: 600; }
QLabel#title { font-size: 25px; font-weight: 650; }
QLabel#muted { color: #718591; }
QLabel#badge { color: #147761; background: #E1F2EB; border-radius: 12px; padding: 7px 12px; }
QFrame#card { background: #FFFFFF; border: 1px solid #E0E8ED; border-radius: 12px; }
QLabel#metric { font-size: 25px; font-weight: 650; }
QLabel#section { font-size: 16px; font-weight: 650; }
QPushButton { background: #FFFFFF; border: 1px solid #D6E1E7; border-radius: 7px; padding: 9px 14px; }
QPushButton:hover { background: #EDF4F5; border-color: #97B9B7; }
QPushButton#primary { background: #147E6E; color: white; border-color: #147E6E; font-weight: 600; }
QPushButton#primary:hover { background: #096C5E; }
QPushButton#danger { color: #B34E4E; }
QPushButton:disabled { color: #A4B1B8; background: #F0F3F5; border-color: #E3E9ED; }
QTableWidget { background: white; border: 1px solid #E0E8ED; border-radius: 10px; selection-background-color: #E4F1EE; selection-color: #173F3D; }
QTableWidget::item { padding: 12px; border-bottom: 1px solid #EDF1F4; }
QHeaderView::section { background: #F8FAFB; color: #748894; border: none; padding: 14px 12px; font-weight: 600; }
QCheckBox { spacing: 9px; padding: 6px 0; }
QCheckBox::indicator { width: 17px; height: 17px; }
QTimeEdit { background: #F7FAFB; border: 1px solid #D4E1E6; border-radius: 8px; padding: 10px 14px; font-size: 24px; min-width: 160px; }
QPlainTextEdit { background: #112D3D; color: #DBEBEE; border: none; border-radius: 10px; padding: 15px; }
QMenu { background: white; padding: 5px; }
QMenu::item { padding: 7px 22px; }
QMenu::item:selected { background: #E4F1EE; }
"""


def label(text, name=None):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    return widget


def button(text, callback, primary=False):
    widget = QPushButton(text)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 18, 22, 20)
    layout.setSpacing(12)
    return frame, layout


class OutputStream(io.TextIOBase):
    def __init__(self, emit):
        self.emit = emit
        self.pending = ""

    def write(self, value):
        self.pending += value
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.emit(line)
        return len(value)

    def flush(self):
        if self.pending.strip():
            self.emit(self.pending)
            self.pending = ""


class Task(QThread):
    line = Signal(str)

    def __init__(self, argv, store, parent=None):
        super().__init__(parent)
        self.argv = argv
        self.store = store
        self.code = 2
        self.cancel_event = threading.Event()
        self.is_login = argv[:2] == ["add", "user"]

    def run(self):
        stream = OutputStream(self.line.emit)
        dry_run = "--dry-run" in self.argv
        def record(account, code):
            self.store.record_result(account, code, dry_run=dry_run)
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                self.code = core.main(self.argv, on_result=record, cancel_event=self.cancel_event)
        except Exception as error:
            self.line.emit(f"任务异常（{type(error).__name__}）。请查看账号状态；不会补发签到请求。")
            self.code = 2
        finally:
            stream.flush()


class MainWindow(QMainWindow):
    def __init__(self, store=None, tray_enabled=True):
        super().__init__()
        self.store = store or DesktopStore()
        self.settings = self.store.settings()
        self.worker = None
        self.pending_quit = False
        self.current_rows = []
        self.controls = []
        self.log_directory = private_dir(self.store.root / "logs")
        self.logger = logging.Logger(f"fib-desktop-{id(self)}")
        handler = RotatingFileHandler(self.log_directory / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        formatter.converter = lambda stamp: datetime.fromtimestamp(stamp, TZ).timetuple()
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.setWindowTitle("FIB 签到助手")
        self.setWindowIcon(QIcon(str(ASSETS / "app.svg")))
        self.resize(1060, 760)
        self.setMinimumSize(940, 650)
        self.setStyleSheet(STYLE)
        self._build_ui()
        self._build_tray(tray_enabled)
        if sys.platform == "darwin":
            quit_action = QAction("退出 FIB 签到助手", self)
            quit_action.setMenuRole(QAction.MenuRole.QuitRole)
            quit_action.setShortcut(QKeySequence.StandardKey.Quit)
            quit_action.triggered.connect(self.request_quit)
            self.menuBar().addMenu("应用").addAction(quit_action)
        self._load_schedule()
        self.refresh()
        path = self.log_directory / "app.log"
        self.logs.setPlainText(path.read_text(encoding="utf-8")[-80_000:])
        self.log("应用已启动。关闭窗口可继续在托盘后台运行。")
        self.timer = QTimer(self)
        self.timer.setInterval(15_000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def _build_ui(self):
        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(192)
        nav = QVBoxLayout(side)
        nav.setContentsMargins(18, 28, 18, 24)
        nav.setSpacing(10)
        nav.addWidget(label("FIB", "brand"))
        nav.addWidget(label("每日签到助手", "sidebarNote"))
        nav.addSpacing(32)
        self.nav_group = QButtonGroup(self)
        for index, title in enumerate(("账号管理", "定时计划", "运行日志")):
            item = QPushButton(title)
            item.setObjectName("nav")
            item.setCheckable(True)
            self.nav_group.addButton(item, index)
            nav.addWidget(item)
        self.nav_group.button(0).setChecked(True)
        # 辅助功能的 checkbox 操作只改变 checked 状态，不一定触发 clicked。
        for index in range(3):
            self.nav_group.button(index).toggled.connect(lambda checked, page=index: self.switch_page(page) if checked else None)
        nav.addStretch()
        nav.addWidget(label("本机运行 · 会话本地保存", "sidebarNote"))
        nav.addWidget(label("北京时间  Asia/Shanghai", "sidebarNote"))
        layout.addWidget(side)
        content = QWidget()
        content.setObjectName("content")
        main = QVBoxLayout(content)
        main.setContentsMargins(28, 28, 28, 20)
        main.setSpacing(18)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        self.page_title = label("账号管理", "title")
        self.page_caption = label("集中管理登录会话，查看今天的签到结果。", "muted")
        titles.addWidget(self.page_title)
        titles.addWidget(self.page_caption)
        header.addLayout(titles)
        header.addStretch()
        self.badge = label("后台守候", "badge")
        header.addWidget(self.badge, alignment=Qt.AlignmentFlag.AlignTop)
        main.addLayout(header)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._accounts_page())
        self.pages.addWidget(self._schedule_page())
        self.pages.addWidget(self._logs_page())
        main.addWidget(self.pages, 1)
        footer = QHBoxLayout()
        self.status = label("准备就绪", "muted")
        footer.addWidget(self.status, 1)
        self.cancel_login = button("取消登录", self.cancel_current_login)
        self.cancel_login.hide()
        footer.addWidget(self.cancel_login)
        self.hide_button = button("收起到托盘", self.hide_to_tray)
        footer.addWidget(self.hide_button)
        main.addLayout(footer)
        layout.addWidget(content, 1)
        self.setCentralWidget(root)

    def _accounts_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        metrics = QHBoxLayout()
        self.metrics = []
        for title in ("已保存账号", "今日已签到", "下次自动签到"):
            frame, inner = card()
            inner.addWidget(label(title, "muted"))
            value = label("—", "metric")
            self.metrics.append(value)
            inner.addWidget(value)
            metrics.addWidget(frame, 1)
        layout.addLayout(metrics)
        actions = QHBoxLayout()
        add = button("＋ 添加账号", self.add_account, True)
        imported = button("导入 Chrome", lambda: self.start_task(["add", "user", "--from-chrome"], "导入 Chrome 会话"))
        dry = button("全部预检查", lambda: self.start_task(["checkin", "--all", "--dry-run"], "批量预检查"))
        run = button("立即全部签到", lambda: self.start_task(["checkin", "--all"], "批量签到"), True)
        actions.addWidget(add)
        actions.addWidget(imported)
        actions.addStretch()
        actions.addWidget(dry)
        actions.addWidget(run)
        self.controls.extend([add, imported, dry, run])
        layout.addLayout(actions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["账号", "登录会话", "今日签到", "最近处理"])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.empty = label("还没有账号。点击“添加账号”，在独立 Chrome 窗口中完成登录。", "muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        selected = QHBoxLayout()
        self.relogin_button = button("重新登录", self.relogin)
        self.rename_button = button("重命名", self.rename)
        self.single_button = button("签到选中账号", self.check_selected)
        self.delete_button = button("移除账号", self.delete)
        self.delete_button.setObjectName("danger")
        for item in (self.relogin_button, self.rename_button, self.single_button):
            selected.addWidget(item)
        selected.addStretch()
        selected.addWidget(self.delete_button)
        self.selection_controls = [self.relogin_button, self.rename_button, self.single_button, self.delete_button]
        self.controls.extend(self.selection_controls)
        layout.addLayout(selected)
        note = label("每个账号每天最多提交一次。会话过期时，可选择账号并重新登录。", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _schedule_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        frame, inner = card()
        inner.addWidget(label("每天自动签到", "section"))
        self.enabled = QCheckBox("启用每日定时签到")
        inner.addWidget(self.enabled)
        row = QHBoxLayout()
        self.daily_time = QTimeEdit()
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setTimeRange(QTime(0, 2), QTime(23, 57))
        row.addWidget(self.daily_time)
        row.addWidget(label("北京时间 · Asia/Shanghai", "muted"))
        row.addStretch()
        inner.addLayout(row)
        self.catch_up = QCheckBox("错过时间后，在启动或电脑唤醒时补签当天计划")
        inner.addWidget(self.catch_up)
        self.plan_summary = label("定时计划尚未启用。", "muted")
        self.plan_summary.setWordWrap(True)
        inner.addWidget(self.plan_summary)
        layout.addWidget(frame)
        frame, inner = card()
        inner.addWidget(label("后台运行", "section"))
        self.start_hidden = QCheckBox("启动时直接收起到托盘（首次没有账号时显示窗口）")
        self.notifications = QCheckBox("后台任务完成或失败时显示通知")
        inner.addWidget(self.start_hidden)
        inner.addWidget(self.notifications)
        note = label("关闭主窗口后仍会继续运行。可通过 macOS 菜单栏或 Windows 托盘打开窗口；选择“退出”后才停止后台任务。", "muted")
        note.setWordWrap(True)
        inner.addWidget(note)
        layout.addWidget(frame)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("保存设置", self.save_schedule, True))
        layout.addLayout(actions)
        note = label("应用退出或电脑关机期间不会执行。开启补签后，下次运行时会检查当天计划；当天自动任务执行过后不会重复发起。", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.enabled.toggled.connect(self.daily_time.setEnabled)
        return page

    def _logs_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        actions.addWidget(label("签到结果与后台事件", "section"))
        actions.addStretch()
        actions.addWidget(button("清空显示", lambda: self.logs.clear()))
        actions.addWidget(button("打开日志目录", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.log_directory)))))
        layout.addLayout(actions)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(2000)
        self.logs.setFont(QFont("Menlo" if sys.platform == "darwin" else "Consolas", 12))
        layout.addWidget(self.logs, 1)
        return page

    def _build_tray(self, enabled):
        self.has_tray = enabled and QSystemTrayIcon.isSystemTrayAvailable()
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("FIB 签到助手 · 后台运行")
        menu = QMenu()
        menu.addAction("显示主窗口", self.show_window)
        menu.addSeparator()
        self.tray_check = menu.addAction("立即全部签到", lambda: self.start_task(["checkin", "--all"], "批量签到"))
        self.tray_dry = menu.addAction("全部预检查", lambda: self.start_task(["checkin", "--all", "--dry-run"], "批量预检查"))
        self.tray_plan = QAction("启用定时签到", self)
        self.tray_plan.setCheckable(True)
        self.tray_plan.triggered.connect(self.toggle_schedule)
        menu.addAction(self.tray_plan)
        menu.addSeparator()
        menu.addAction("退出", self.request_quit)
        self.tray.setContextMenu(menu)
        self.tray_menu = menu
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        if self.has_tray:
            self.tray.show()
        self.hide_button.setEnabled(self.has_tray)

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        titles = ("账号管理", "定时计划", "运行日志")
        captions = ("集中管理登录会话，查看今天的签到结果。", "设置每日签到时间，让应用在后台按时处理。", "查看每个账号的处理结果，快速定位需要处理的问题。")
        self.page_title.setText(titles[index])
        self.page_caption.setText(captions[index])

    def selected(self):
        row = self.table.currentRow()
        return self.current_rows[row] if 0 <= row < len(self.current_rows) else None

    def _selection_changed(self):
        for widget in self.selection_controls:
            widget.setEnabled(self.selected() is not None and self.worker is None)

    def refresh(self):
        old = (self.selected() or {}).get("id")
        saved = users.list_users()
        self.current_rows = account_rows(saved, self.store, core.STATE_DIR)
        self.table.setRowCount(len(self.current_rows))
        for index, row in enumerate(self.current_rows):
            when = "—"
            if row["last_at"]:
                try:
                    when = datetime.fromisoformat(row["last_at"]).astimezone(TZ).strftime("%m-%d %H:%M")
                except ValueError:
                    pass
            values = [row["name"] + "\n" + row["id"][:8], row["session"], row["today"], when]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2:
                    item.setForeground(QColor("#147E6E" if value == "已签到" else "#A56B29" if "确认" in value or "处理" in value and value != "尚未处理" else "#718591"))
                self.table.setItem(index, column, item)
            self.table.setRowHeight(index, 68)
            if row["id"] == old:
                self.table.selectRow(index)
        self.empty.setVisible(not self.current_rows)
        self.metrics[0].setText(str(len(saved)))
        self.metrics[1].setText(str(sum(row["today"] == "已签到" for row in self.current_rows)))
        self.update_plan_summary()
        self._selection_changed()

    def _load_schedule(self):
        self.settings = self.store.settings()
        self.enabled.setChecked(self.settings["enabled"])
        hour, minute = map(int, self.settings["time"].split(":"))
        self.daily_time.setTime(QTime(hour, minute))
        self.daily_time.setEnabled(self.settings["enabled"])
        self.catch_up.setChecked(self.settings["catch_up"])
        self.start_hidden.setChecked(self.settings["start_hidden"])
        self.notifications.setChecked(self.settings["notifications"])
        self.tray_plan.setChecked(self.settings["enabled"])

    def update_plan_summary(self):
        target = self.store.next_run()
        self.metrics[2].setText(target.strftime("%H:%M") if target else "未启用")
        if target:
            self.plan_summary.setText(f"下次计划：{target.strftime('%m 月 %d 日 %H:%M')}（北京时间），处理全部已保存账号。")
        else:
            self.plan_summary.setText("定时计划尚未启用，可随时手动签到或预检查。")

    def save_schedule(self):
        try:
            self.settings = self.store.save_settings({
                "enabled": self.enabled.isChecked(), "time": self.daily_time.time().toString("HH:mm"),
                "catch_up": self.catch_up.isChecked(), "start_hidden": self.start_hidden.isChecked(),
                "notifications": self.notifications.isChecked(),
            })
            self.tray_plan.setChecked(self.settings["enabled"])
            self.update_plan_summary()
            self.log(f"设置已保存：{'每天 ' + self.settings['time'] + ' 自动签到' if self.settings['enabled'] else '定时签到已暂停'}。")
            self.status.setText("设置已保存")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "设置未保存", str(error))

    def toggle_schedule(self, enabled):
        try:
            self.store.save_settings({"enabled": enabled})
            self._load_schedule()
            self.update_plan_summary()
            self.log("定时签到已启用。" if enabled else "定时签到已暂停。")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "设置未保存", str(error))

    def add_account(self):
        name, okay = QInputDialog.getText(self, "添加账号", "账号名称（可留空，登录后自动识别）：")
        if okay:
            self.start_task(["add", "user"] + ([name.strip()] if name.strip() else []), "等待 FIB 登录")

    def relogin(self):
        selected = self.selected()
        if selected:
            self.start_task(["add", "user", selected["name"]], f"重新登录 {selected['name']}")

    def rename(self):
        selected = self.selected()
        if not selected:
            return
        name, okay = QInputDialog.getText(self, "重命名账号", "新的账号名称：", text=selected["name"])
        if okay:
            try:
                users.rename_user(selected["id"], name)
                self.refresh()
            except (users.UserError, OSError) as error:
                QMessageBox.warning(self, "无法重命名", str(error))

    def delete(self):
        selected = self.selected()
        if not selected:
            return
        answer = QMessageBox.question(self, "移除本机账号", f"移除“{selected['name']}”的本地登录会话？\n签到记录会保留，FIB 网站账号不会删除。")
        if answer == QMessageBox.StandardButton.Yes:
            try:
                users.delete_user(selected["id"])
                self.log(f"已移除账号：{selected['name']}；签到记录已保留。")
                self.refresh()
            except (users.UserError, OSError) as error:
                QMessageBox.warning(self, "无法移除账号", str(error))

    def check_selected(self):
        selected = self.selected()
        if selected:
            self.start_task(["checkin", "--user", selected["id"]], "签到选中账号")

    def log(self, text):
        self.logger.info(text)
        self.logs.appendPlainText(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {text}")

    def start_task(self, argv, title):
        if self.worker is not None or self.pending_quit:
            return False
        self.status.setText(title)
        self.badge.setText("正在处理")
        for widget in self.controls:
            widget.setEnabled(False)
        self.tray_check.setEnabled(False)
        self.tray_dry.setEnabled(False)
        self.worker = Task(argv, self.store, self)
        self.worker.line.connect(self.log)
        self.worker.finished.connect(self._task_finished)
        self.cancel_login.setVisible(self.worker.is_login)
        self.cancel_login.setEnabled(True)
        self.log(title + "。")
        self.worker.start()
        return True

    def _task_finished(self):
        worker = self.worker
        code = worker.code
        self.worker = None
        worker.deleteLater()
        self.cancel_login.hide()
        for widget in self.controls:
            widget.setEnabled(True)
        self.tray_check.setEnabled(True)
        self.tray_dry.setEnabled(True)
        self.badge.setText("后台守候")
        self.status.setText("处理完成" if code == 0 else "已取消登录" if code == 130 else "有账号需要处理，请查看运行日志")
        try:
            self.refresh()
        except (ValueError, OSError, users.UserError) as error:
            self.log(f"刷新账号状态失败：{error}")
        if self.pending_quit:
            self.tray.hide()
            QApplication.instance().quit()
        elif code != 130 and not self.isVisible() and self.has_tray and self.settings["notifications"]:
            self.tray.showMessage("FIB 签到助手", "任务已完成。" if code == 0 else "有账号需要处理，请打开运行日志查看。", QSystemTrayIcon.MessageIcon.Information if code == 0 else QSystemTrayIcon.MessageIcon.Warning)

    def cancel_current_login(self):
        if self.worker is not None and self.worker.is_login:
            self.worker.cancel_event.set()
            self.cancel_login.setEnabled(False)
            self.status.setText("正在取消登录并关闭独立窗口……")

    def tick(self):
        if self.pending_quit or self.worker is not None:
            return
        try:
            self.update_plan_summary()
            if self.store.claim_due(busy=self.worker is not None, has_users=bool(users.list_users())):
                self.log("每日定时任务已触发。")
                self.start_task(["checkin", "--all"], "自动批量签到")
            else:
                self.refresh()
        except users.UserBusy:
            return  # 短暂锁竞争在下次计时器检查时再读取，不影响定时计划。
        except (ValueError, OSError, users.UserError) as error:
            self.timer.stop()
            self.log(f"调度已暂停，请检查设置或数据目录：{error}")
            self.status.setText("调度已暂停，请查看运行日志")

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self):
        if self.has_tray:
            self.hide()

    def request_quit(self):
        self.timer.stop()
        if self.worker is not None:
            self.pending_quit = True
            if self.worker.is_login:
                self.cancel_current_login()
            self.status.setText("正在等待当前任务安全结束，然后退出……")
            self.log("收到退出请求，将等待当前任务结束后退出。")
            return
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event):
        if self.has_tray and not self.pending_quit:
            event.ignore()
            self.hide()
        elif self.worker is not None:
            event.ignore()
            self.request_quit()
        else:
            event.accept()
            QApplication.instance().quit()


def smoke_test(app, root):
    """只渲染界面和启动本地驱动；不得登录或发起签到。"""
    window = None
    result = {"ok": False, "packaged": bool(getattr(sys, "frozen", False)), "platform": sys.platform}
    try:
        window = MainWindow(DesktopStore(root), tray_enabled=False)
        window.timer.stop()
        if window.current_rows or window.settings["enabled"]:
            raise ValueError("smoke test requires an empty isolated data directory")
        raw_font = QRawFont.fromFont(app.font())
        if not all(raw_font.supportsCharacter(ord(char)) for char in "FIB09签到助手"):
            raise ValueError("bundled font cannot render Latin digits or Chinese UI text")
        window.show()
        for index, name in enumerate(("accounts", "schedule", "logs")):
            window.switch_page(index)
            app.processEvents()
            if not window.grab().save(str(root / f"smoke-{name}.png")):
                raise OSError("cannot save smoke screenshot")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            if playwright.chromium.name != "chromium":
                raise RuntimeError("Playwright driver failed to initialize")
        if list((root / ".state").glob("*.json")):
            raise RuntimeError("smoke test unexpectedly created a checkin record")
        result.update(ok=True, pages=3, playwright_driver=True, signup_requests=0, font_family=raw_font.familyName(), chinese_glyphs=True)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        if window is not None:
            window.close()
            for handler in window.logger.handlers:
                handler.close()
        atomic_json(root / "smoke-result.json", result)
    return 0 if result["ok"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="启动时显示主窗口")
    parser.add_argument("--background", action="store_true", help="启动到系统托盘")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_test and (not os.environ.get("FIB_DATA_DIR") or os.environ.get("QT_QPA_PLATFORM") != "offscreen"):
        parser.error("--smoke-test requires FIB_DATA_DIR and QT_QPA_PLATFORM=offscreen")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("FIB Checkin")
    app.setApplicationDisplayName("FIB 签到助手")
    app.setOrganizationName("FIB Checkin")
    app.setQuitOnLastWindowClosed(False)
    try:
        configure_font(app)
        root = initialize_data()
        if args.smoke_test:
            return smoke_test(app, root)
        lock = QLockFile(str(root / "desktop.lock"))
        lock.setStaleLockTime(0)
        server_name = "fib-checkin-" + hashlib.sha256(str(root).encode()).hexdigest()[:20]
        if not lock.tryLock(0):
            socket = QLocalSocket()
            socket.connectToServer(server_name)
            if socket.waitForConnected(1000):
                socket.write(b"show")
                socket.waitForBytesWritten(1000)
            else:
                QMessageBox.information(None, "FIB 签到助手", "应用已在后台运行，可从系统托盘打开。")
            return 0
        window = MainWindow(DesktopStore(root))
        server = QLocalServer()
        QLocalServer.removeServer(server_name)
        if not server.listen(server_name):
            raise OSError("无法启动本地窗口唤起服务。")
        def activate():
            socket = server.nextPendingConnection()
            window.show_window()
            socket.disconnectFromServer()
            socket.deleteLater()
        server.newConnection.connect(activate)
        hidden = args.background or (window.settings["start_hidden"] and bool(window.current_rows))
        if args.show or not hidden or not window.has_tray:
            window.show_window()
        QTimer.singleShot(1000, window.tick)
        code = app.exec()
        server.close()
        lock.unlock()
        return code
    except (ValueError, OSError, users.UserError) as error:
        QMessageBox.critical(None, "无法启动 FIB 签到助手", str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
