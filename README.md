# FIB 自动签到助手

macOS / Windows 桌面与托盘应用，支持图形化账号管理、每天定时签到、手动批量签到和处理日志，也可以使用 Python 命令行。

## 桌面应用

在仓库的 [Actions](https://github.com/cylemonVip/fib-auto-checkin/actions/workflows/desktop-build.yml) 中选择成功的构建，下载对应的 `FIB-Checkin-windows-latest` 或 `FIB-Checkin-macos-14` artifact。

解压 artifact 后，再解压其中的应用 ZIP。Windows 双击文件夹中的 `FIB Checkin.exe`，请保留旁边的运行库；macOS 双击 `FIB Checkin.app`。应用不需要另行安装 Python，添加或重新登录账号时需要电脑已安装 Google Chrome。

- 添加账号时打开独立 Chrome 登录窗口，手动登录后自动保存会话；不保存密码。
- 日常签到不需要 Chrome 保持打开。
- 启用定时计划后，每天按设定的北京时间签到，可在启动或电脑唤醒后补做当天错过的计划。
- 主窗口可以收起到托盘，关闭窗口后继续运行；从托盘恢复窗口或退出应用。
- 每个实际 FIB 账号每天最多提交一次；网络异常、超时或崩溃后不自动补发签到请求。

定时计划默认关闭。应用需保持运行，退出应用或关机期间不会签到。详细使用、数据迁移和构建说明见 [DESKTOP.md](DESKTOP.md)。

## 从源码运行

需要 Python 3.12：

```bash
python -m pip install -r requirements-desktop.txt
python fib_desktop.py --show
```

## 命令行

```bash
# 打开新窗口登录并保存账号；名称可省略
python fib_checkin.py add user 主账号

# 查看、删除本地账号；删除会话不会清除当天提交记录
python fib_checkin.py list users
python fib_checkin.py delete user 主账号

# 批量签到；不带参数运行也执行全部已保存账号
python fib_checkin.py checkin --all

# 指定账号，或只做预检查
python fib_checkin.py checkin --user 主账号
python fib_checkin.py checkin --all --dry-run

# 从已有 Chrome 配置导入会话（使用实际配置目录名称）
python fib_checkin.py add user 主账号 --from-chrome --chrome-profile Default
```

登录过期时，重新执行 `add user 账号名称` 并手动登录即可。同一实际账号再次添加会更新会话，不会创建重复账号。

## 本地数据

桌面版与命令行工具共享用户应用数据目录：

- macOS：`~/Library/Application Support/FIB Checkin/`
- Windows：`%LOCALAPPDATA%\FIB Checkin\`

`.users/` 保存登录会话，`.state/` 保存每日提交记录。会话文件包含敏感 Cookie，请勿分享；保留 `.state/`，删除、重命名或重新添加账号不能绕过已有的每日记录。首次从旧版项目启动时，会复制迁移旧账号和提交记录，原文件保留。

本仓库和构建产物不包含用户账号、Cookie、密码、日志或签到记录。可使用 `FIB_DATA_DIR` 指定独立的测试目录。

## 测试与打包

```bash
python -m pip install -r requirements-build.txt
python -m unittest
python ci_smoke.py --source
python build_desktop.py
python ci_smoke.py
```

在目标操作系统上构建应用，结果位于 `release/`。推送 `desktop-v*` 标签或手动运行 GitHub Actions，会分别在 macOS / Windows runner 执行离线测试、打包和打包后启动检查。

测试仅使用临时账号夹具；启动检查使用隔离目录，只渲染界面和启动 Playwright 本地驱动，不访问 FIB 或签到。
