# FIB 自动签到助手

macOS / Windows 桌面与托盘应用，支持图形化账号管理、每天定时签到、手动批量签到和处理日志，也可以使用 Python 命令行。

## 桌面应用

从 [GitHub Releases](https://github.com/cylemonVip/fib-auto-checkin/releases/latest) 直接下载应用，无需安装 Python：

| 平台 | 下载 | 使用方式 |
| --- | --- | --- |
| Windows 64 位 | [下载 Windows 版](https://github.com/cylemonVip/fib-auto-checkin/releases/latest/download/FIB-Checkin-Windows-x64.zip) | 解压后双击 `FIB Checkin.exe`，保留同目录运行库 |
| macOS 14+，Apple Silicon | [下载 macOS 版](https://github.com/cylemonVip/fib-auto-checkin/releases/latest/download/FIB-Checkin-macOS-arm64.zip) | 解压后将 `FIB Checkin.app` 移入“应用程序”，双击启动 |

添加或重新登录账号时需要电脑已安装 Google Chrome。当前 macOS 包使用开发签名，未做 Apple 分发公证；首次启动若被系统拦截，请在“系统设置 → 隐私与安全性”中为这个已下载的应用选择“仍要打开”。Windows 包未做商业代码签名，可能显示发布者未验证提示。

- 添加账号时打开独立 Chrome 登录窗口，默认勾选“记住我”，手动登录后自动保存会话。
- 可按账号启用自动登录：先验证邮箱密码，密码仅存入 macOS 钥匙串 / Windows 凭据管理器；会话过期时在签到前恢复登录。默认关闭，不启用时不保存密码。
- 账号列表显示会话到期时间；关闭自动登录或移除账号会删除系统保存的密码。
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

# 可选：验证并启用自动登录；密码在终端隐藏输入，不放在命令参数中
python fib_checkin.py autologin user 主账号 --email user@example.com

# 关闭自动登录并删除系统保存的密码
python fib_checkin.py autologin user 主账号 --disable

# 批量签到；不带参数运行也执行全部已保存账号
python fib_checkin.py checkin --all

# 指定账号，或只做预检查
python fib_checkin.py checkin --user 主账号
python fib_checkin.py checkin --all --dry-run

# 从已有 Chrome 配置导入会话（使用实际配置目录名称）
python fib_checkin.py add user 主账号 --from-chrome --chrome-profile Default
```

未启用自动登录时，登录过期后重新执行 `add user 账号名称` 并手动登录即可。同一实际账号再次添加会更新会话，不会创建重复账号。

自动登录仅在实际签到流程确认会话失效时触发。`--dry-run` 预检查始终只读，不会提交登录或签到。自动恢复失败、超时或需要两步验证 / 验证码时会停止，30 分钟内不会再次自动尝试；不会重发已经提交过的签到请求。修改密码后请重新配置自动登录。

## 本地数据

桌面版与命令行工具共享用户应用数据目录：

- macOS：`~/Library/Application Support/FIB Checkin/`
- Windows：`%LOCALAPPDATA%\FIB Checkin\`

`.users/` 保存登录会话、自动登录开关和恢复尝试时间，`.state/` 保存每日提交记录。密码仅存在系统凭据库，不写入这些文件。会话文件包含敏感 Cookie，请勿分享；保留 `.state/`，删除、重命名或重新添加账号不能绕过已有的每日记录。首次从旧版项目启动时，会复制迁移旧账号和提交记录，原文件保留。

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

正式发布使用 **Publish verified desktop release** 工作流：将成功构建的 run ID、源码提交和应用包 SHA-256 记录在 `.github/releases/<版本>.json`，附上同名发布说明，再推送对应的 `v*` 标签。工作流校验构建结果和文件哈希后，把应用包与 `SHA256SUMS.txt` 上传到 Releases。

测试仅使用临时账号夹具；启动检查使用隔离目录，渲染界面、启动 Playwright 本地驱动，并用临时测试凭据检查系统密码库的保存 / 读取 / 删除。测试凭据随检查删除，macOS CI 使用独立测试钥匙串；不访问 FIB 或签到。
