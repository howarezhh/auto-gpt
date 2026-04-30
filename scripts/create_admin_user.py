from __future__ import annotations

import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SessionLocal
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


def _prompt_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"{label}不能为空")


def _prompt_password() -> str:
    while True:
        password = getpass.getpass("管理员密码: ")
        password_confirm = getpass.getpass("确认密码: ")
        if password != password_confirm:
            print("两次输入的密码不一致")
            continue
        return password


def main() -> int:
    print("创建管理员账号")
    username = _prompt_required("管理员用户名")
    email = _prompt_required("管理员邮箱")
    password = _prompt_password()

    db = SessionLocal()
    try:
        user = UserAuthService.create_user(
            db,
            username=username,
            email=email,
            password=password,
            role=USER_ROLE_ADMIN,
            enabled=True,
        )
    except ValueError as exc:
        print(f"创建失败：{exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"管理员创建成功：{user.username} <{user.email}>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
