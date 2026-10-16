"""仅通过 macOS 钥匙串 / Windows 凭据管理器保存自动登录密码。"""

import hashlib
import json
import sys

from fib_users import UserError


def backend():
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            selected = Keyring()
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring
            selected = WinVaultKeyring()
        else:
            raise UserError("自动登录仅支持 macOS 钥匙串或 Windows 凭据管理器。")
        if selected.priority <= 0:
            raise ValueError()
        return selected
    except UserError:
        raise
    except Exception:
        raise UserError("系统凭据库不可用，未保存密码；请检查系统权限和 keyring 依赖。") from None


def service():
    import fib_users
    namespace = hashlib.sha256(str(fib_users.USER_DIR.resolve()).encode()).hexdigest()[:20]
    return "FIB Checkin Auto Login " + namespace


def save(account, email, password):
    try:
        backend().set_password(service(), account, json.dumps({"email": email, "password": password}, ensure_ascii=False))
    except Exception:
        raise UserError("无法把密码保存到系统凭据库；未启用自动登录。") from None


def load(account):
    try:
        value = backend().get_password(service(), account)
        if value is None:
            raise ValueError()
        data = json.loads(value)
        email, password = data["email"], data["password"]
        if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
            raise ValueError()
        return email, password
    except Exception:
        raise UserError("无法读取自动登录凭据，请解锁系统凭据库或重新设置自动登录。") from None


def delete(account):
    try:
        selected = backend()
        if selected.get_password(service(), account) is not None:
            selected.delete_password(service(), account)
    except Exception:
        raise UserError("无法删除系统保存的密码，请解锁凭据库后重试。") from None
