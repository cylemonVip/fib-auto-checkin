# FIB 签到助手桌面版

桌面版支持 macOS 和 Windows，沿用命令行版本的签到接口、每日提交记录和防重复机制。

## 使用

从 [GitHub Releases](https://github.com/cylemonVip/fib-auto-checkin/releases/latest) 下载对应平台的应用 ZIP。macOS 解压后将 `FIB Checkin.app` 移入“应用程序”并双击；Windows 解压后双击文件夹中的 `FIB Checkin.exe`，请保留同目录的运行库文件。应用内置 Python，不需要用户安装开发环境。

已保存账号且启用“启动时直接收起到托盘”时，应用启动后在后台运行。点击 macOS 菜单栏或 Windows 通知区域里的 FIB 图标，选择“显示主窗口”。首次没有账号时会显示账号管理窗口。再次启动应用会唤起已经运行的实例。

- **账号管理**：添加、重新登录、重命名、移除账号；支持批量预检查、批量签到和单账号签到。
- **添加账号**：打开独立 Google Chrome 窗口，手动登录后自动保存会话并关闭窗口。登录窗口阻止签到请求。此步骤需要已安装 Chrome，日常签到无需 Chrome 开着。
- **定时计划**：设置每天的签到时间，时间按北京时间 `Asia/Shanghai` 显示。默认不启用，保存并启用后才会自动执行。
- **错过时间后补签**：应用启动或电脑从睡眠唤醒后，处理当天尚未发起的计划；不会追补过去日期。
- **后台运行**：关闭主窗口会收起到托盘并继续调度。选择托盘菜单“退出”才会停止；有任务进行时，会等当前任务安全结束后退出。
- **运行日志**：查看处理过程与结果，后台任务结束后可显示通知。

定时签到需要应用保持运行。应用退出、电脑关机或睡眠期间不会执行，也不会主动唤醒电脑。未设置系统开机自启动。

## 数据与旧版迁移

应用和新版命令行工具共享同一份本地数据：

- macOS：`~/Library/Application Support/FIB Checkin/`
- Windows：`%LOCALAPPDATA%\FIB Checkin\`

其中 `.users/users.json` 保存登录 Cookie 和账号名称；`.state/` 保存实际 FIB 账号的每日提交记录；`desktop.json` 保存设置和当日自动任务标记；`logs/app.log` 保存日志，超过大小限制后轮换保留。

从本项目首次启动新版时，会先复制旧 `.state/` 记录，再复制旧 `.users/` 账号。原文件会保留，已迁移的数据不会被重复导入，移除的账号不会因下次启动重新出现。实际构建包不包含个人账号、Cookie 或签到记录。

macOS 上数据目录仅当前用户可访问；Windows 使用当前用户的 LocalAppData 目录并继承其权限。请勿分享登录会话文件。删除、重命名或重新登录账号都不会清除当天的提交记录。

## 开发运行

```bash
python -m pip install -r requirements-desktop.txt
python fib_desktop.py --show
```

后台启动：`python fib_desktop.py --background`。参数 `--show` 强制显示窗口。同一数据目录只允许一个桌面实例运行。

可用 `FIB_DATA_DIR` 指定独立数据目录，适合测试；显式指定目录时不会迁入真实账号。

## 打包

在目标操作系统执行：

```bash
python -m pip install -r requirements-build.txt
python -m unittest
python build_desktop.py
python ci_smoke.py
```

构建使用 PyInstaller，运行包内置 Python、Qt 和 Playwright 驱动。Chrome 不内置，添加账号时使用电脑上已安装的 Chrome。

应用内置 Noto Sans SC 字体，保证系统未安装中文字体时界面也能正确显示。字体来源为 Google Fonts，OFL 许可证随应用保存在 `assets/fonts/OFL.txt`。

macOS 构建生成 `dist/FIB Checkin.app` 和 `release/FIB-Checkin-macOS-<架构>.zip`。本机构建为 Apple Silicon 版本，Qt 运行库要求 macOS 14 或更新版本；Intel Mac 应在 Intel 环境重新构建。当前为本地开发签名，未做 Apple 分发公证。

Windows 构建生成 `dist/FIB Checkin/FIB Checkin.exe` 和 `release/FIB-Checkin-Windows-x64.zip`。请在 Windows x64、Python 3.12 环境构建；macOS 无法用 PyInstaller 直接生成 Windows 可执行文件。

已提供 `.github/workflows/desktop-build.yml`，将源码放入 GitHub 仓库后，可手动运行 **Build desktop applications**，分别在 macOS 和 Windows runner 测试和打包，再下载对应 ZIP。工作流会执行离线测试和打包后的启动检查；构建产物从本仓库 Actions 页面下载。

## 验证范围

离线测试覆盖账号管理、每日防重复、并发锁、调度触发、重启后不重复调度、睡眠补签、设置持久化、旧版数据迁移和 Qt 后台任务。测试不会发送真实签到请求。macOS 应用另外执行本机界面与打包运行验证；Windows 原生构建和打包启动检查由 GitHub Actions 的 Windows runner 执行。两平台均检查打包后的三个界面、内置字体的中英文字形和 Playwright 驱动初始化，并保存截图与检查报告。

实现参考：[Qt 托盘接口](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QSystemTrayIcon.html)、[PyInstaller 的平台构建说明](https://pyinstaller.org/en/stable/usage.html#supporting-multiple-operating-systems)、[Playwright 打包说明](https://playwright.dev/python/docs/library#pyinstaller)。
