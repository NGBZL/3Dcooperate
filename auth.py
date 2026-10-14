"""
登录与权限
----------
看板部署在局域网里、6 个人从不同电脑的浏览器访问，因此需要：

* **登录**：用户名（代号）+ 密码，密码用 werkzeug 的哈希存储，绝不存明文
* **会话**：Flask 自带 session（服务端只存签名后的 cookie，密钥来自 config.SECRET_KEY）
* **权限**：每个人只能改「负责人是自己」的任务；「完成」只能由 Agent 写入

关于姓名：users 表里存的是**代号**（小明…），真实姓名只存在于本地
assignees.local.json（已被 .gitignore 忽略），由 display_name_of() 在显示时翻译。
这样数据库、日志、GitHub 上都不会出现真名。

对外主要接口：
    init_app(app)             配置 session / 密钥
    ensure_default_users()    首次启动时创建 6 个用户
    verify_login(...)         校验用户名密码（带失败锁定）
    login_required            页面装饰器：未登录跳转 /login
    api_login_required        API 装饰器：未登录返回 401 JSON
"""

from __future__ import annotations

import logging
import threading
import time
from functools import wraps
from typing import Any, Callable, Optional

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import database
from config import (
    ALLOW_ASSIGN_TO_OTHERS,
    ASSIGNEES,
    DEFAULT_PASSWORD,
    LOGIN_BY_REAL_NAME_ONLY,
    LOGIN_LOCK_SECONDS,
    LOGIN_MAX_ATTEMPTS,
    MIN_PASSWORD_LENGTH,
    SESSION_LIFETIME_HOURS,
    assignee_aliases,
    assignee_display,
    clean_assignee,
    normalize_assignee,
)

logger = logging.getLogger(__name__)

# 会话里存放登录名的键
SESSION_USER_KEY = "username"

# 角色：
#   member —— 普通组员：只能领取任务、推进自己名下的任务
#   lead   —— 组长：可以给任何人分派任务、改任何卡片、放行并合并 PR
ROLE_MEMBER = "member"
ROLE_LEAD = "lead"
ROLES = (ROLE_MEMBER, ROLE_LEAD)

# 也允许用「真名」登录：真名 -> 代号 的反向查找由 config.assignee_code() 完成

# 登录失败计数：{代号: (失败次数, 锁定到什么时候)}，进程内存即可（单机部署）
_failed_logins: dict[str, tuple[int, float]] = {}
_login_lock = threading.Lock()


def init_app(app) -> None:
    """
    给 Flask app 装上会话配置。

    :param app: Flask 应用
    """
    from config import SECRET_KEY

    app.secret_key = SECRET_KEY
    app.config.update(
        # 只允许 httpOnly，防止前端脚本读取
        SESSION_COOKIE_HTTPONLY=True,
        # 局域网内是 http 访问，不能开 Secure，否则 cookie 根本发不出去
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=int(SESSION_LIFETIME_HOURS * 3600),
    )


# ============================================================
# 初始用户
# ============================================================
def ensure_default_users() -> list[str]:
    """
    确保 6 个代号用户都存在（不存在则用初始密码创建）。

    :return: 本次新建的登录名列表
    """
    created: list[str] = []

    for code in ASSIGNEES:
        if database.get_user(code):
            continue
        database.create_user(code, generate_password_hash(DEFAULT_PASSWORD))
        created.append(code)
        logger.info("已创建初始用户：%s（初始密码见 config.DEFAULT_PASSWORD）", code)

    if created:
        logger.warning("=" * 56)
        logger.warning("已创建 %d 个初始用户，初始密码统一为：%s", len(created), DEFAULT_PASSWORD)
        logger.warning("请提醒大家登录后在右上角「修改密码」里改掉")
        logger.warning("=" * 56)

    return created


# ============================================================
# 角色（组长 / 组员）
# ============================================================
def user_role(username: str) -> str:
    """
    取用户角色（数据库里没有记录时按组员处理）。

    :param username: 登录名（代号）
    :return: 'lead' 或 'member'
    """
    if not username:
        return ROLE_MEMBER
    user = database.get_user(username)
    role = (user or {}).get("role") or ROLE_MEMBER
    return role if role in ROLES else ROLE_MEMBER


def is_lead(username: str) -> bool:
    """是否是组长。"""
    return user_role(username) == ROLE_LEAD


def lead_only_message(action: str = "这个操作") -> str:
    """非组长做组长专属操作时的提示。"""
    return f"{action}只有组长可以做（让组长登录后操作，或用 python auth.py role 设为 lead）"


def check_lead(username: str) -> str:
    """
    校验「只有组长能做」的操作。

    :param username: 当前登录名
    :return: 错误信息；空串表示允许
    """
    if not username:
        return "请先登录"
    if is_lead(username):
        return ""
    return lead_only_message()


# ============================================================
# 登录校验
# ============================================================
def _locked_for(username: str) -> int:
    """
    当前还剩多少秒处于锁定期。

    注意：`locked_until == 0` 表示「有失败记录但还没到锁定阈值」，
    这种情况下**不能清除计数**，否则计数永远停在 1、阈值永远触发不了。

    :param username: 登录名
    :return: 剩余秒数；0 表示未锁定
    """
    with _login_lock:
        entry = _failed_logins.get(username)
        if not entry:
            return 0

        _, locked_until = entry
        if locked_until <= 0:
            return 0

        remaining = int(locked_until - time.time())
        if remaining <= 0:
            # 锁定期已过 → 清空计数，允许重新尝试
            _failed_logins.pop(username, None)
            return 0
        return remaining


def _record_failure(username: str) -> None:
    """记一次登录失败，达到阈值则锁定一段时间。"""
    with _login_lock:
        count, _ = _failed_logins.get(username, (0, 0.0))
        count += 1
        locked_until = time.time() + LOGIN_LOCK_SECONDS if count >= LOGIN_MAX_ATTEMPTS else 0.0
        _failed_logins[username] = (count, locked_until)
        if locked_until:
            logger.warning("用户 %s 连续登录失败 %d 次，已锁定 %d 秒",
                           username, count, LOGIN_LOCK_SECONDS)


def _clear_failures(username: str) -> None:
    """登录成功后清空失败计数。"""
    with _login_lock:
        _failed_logins.pop(username, None)


def resolve_username(raw: str) -> str:
    """
    把用户输入折算成登录名（代号）——**内部/管理员用**，代号和真名都认。

    例如命令行工具重置密码时，管理员写 `python auth.py reset 小美` 或
    `python auth.py reset 张三` 都应该能用。

    :param raw: 用户输入的账号
    :return: 代号；无法识别时返回空串
    """
    name = clean_assignee(raw)
    if not name:
        return ""

    # normalize_assignee 内部会先查代号、再查真名映射
    return normalize_assignee(name)


def resolve_login_username(raw: str) -> str:
    """
    把登录框里的输入折算成登录名（代号）——**登录接口用**。

    默认只认「真实姓名」：代号太难听，不该让组员去记。
    两种例外会自动回退到「代号也能登录」，避免整站登不进去：

    * 配置里显式关掉了 `LOGIN_BY_REAL_NAME_ONLY`
    * 本地真名映射表不存在/为空（说明还没配真名，此时只认代号）

    :param raw: 登录框输入
    :return: 代号；无法识别时返回空串
    """
    name = clean_assignee(raw)
    if not name:
        return ""

    aliases = assignee_aliases()          # {代号: 真名}

    # ① 真名优先
    for code, real_name in aliases.items():
        if name == real_name:
            return code

    # ② 代号：只在允许、或没有真名映射时接受
    if name in ASSIGNEES:
        if not LOGIN_BY_REAL_NAME_ONLY:
            return name
        if not aliases:
            logger.warning("真名映射表为空，暂时允许用代号 %s 登录（请补全 assignees.local.json）", name)
            return name
        logger.warning("有人尝试用代号登录，已拒绝（请用真实姓名登录）")
        return ""

    return ""


def verify_login(raw_username: str, password: str) -> tuple[Optional[dict[str, Any]], str]:
    """
    校验用户名与密码。

    :param raw_username: 用户输入的账号（默认真实姓名，见 resolve_login_username）
    :param password: 明文密码
    :return: (用户字典, 错误信息)；成功后错误信息为空串
    """
    username = resolve_login_username(raw_username)
    if not username:
        # 不区分“用户不存在”和“密码错误”，避免被枚举账号
        return None, "用户名或密码不正确"

    remaining = _locked_for(username)
    if remaining:
        return None, f"登录失败次数过多，请 {remaining} 秒后再试"

    user = database.get_user(username)
    if not user or not password:
        _record_failure(username)
        database.log_operation(username, "login_failed", result="denied",
                               detail="账号不存在或密码为空")
        return None, "用户名或密码不正确"

    if not check_password_hash(user["password_hash"], password):
        _record_failure(username)
        database.log_operation(username, "login_failed", result="denied", detail="密码错误")
        return None, "用户名或密码不正确"

    _clear_failures(username)
    return user, ""


def change_password(username: str, old_password: str, new_password: str) -> str:
    """
    修改密码。

    :param username: 登录名
    :param old_password: 原密码
    :param new_password: 新密码
    :return: 错误信息；空串表示成功
    """
    user = database.get_user(username)
    if not user:
        return "用户不存在"
    if not check_password_hash(user["password_hash"], old_password or ""):
        return "原密码不正确"
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        return f"新密码至少 {MIN_PASSWORD_LENGTH} 位"
    if new_password == old_password:
        return "新密码不能与原密码相同"

    database.set_user_password(username, generate_password_hash(new_password))
    return ""


def is_default_password(username: str) -> bool:
    """
    判断该用户是否还在用初始密码（用于前端提醒）。

    :param username: 登录名
    :return: True 表示仍是初始密码
    """
    user = database.get_user(username)
    if not user:
        return False
    try:
        return check_password_hash(user["password_hash"], DEFAULT_PASSWORD)
    except ValueError:
        return False


# ============================================================
# 当前登录状态
# ============================================================
def current_username() -> str:
    """
    取当前会话的登录名。

    :return: 登录名；未登录时返回空串
    """
    return str(session.get(SESSION_USER_KEY) or "")


def current_user() -> Optional[dict[str, Any]]:
    """
    取当前登录用户的完整信息（含给前端看的显示名）。

    :return: 用户信息字典；未登录时返回 None
    """
    username = current_username()
    if not username:
        return None
    user = database.get_user(username)
    if not user:
        # 用户被删掉了但会话还在 → 视为未登录
        session.pop(SESSION_USER_KEY, None)
        return None
    return user


def user_public_info(user: dict[str, Any]) -> dict[str, Any]:
    """
    转成可以安全返回给前端的用户信息（**不含密码哈希**）。

    :param user: users 表里的用户字典
    :return: {username, display_name, is_default_password}
    """
    username = user["username"]
    role = user.get("role") or ROLE_MEMBER
    if role not in ROLES:
        role = ROLE_MEMBER
    return {
        "username": username,
        # 真名由本地映射表翻译，数据库里不存真名
        "display_name": assignee_display(username) or username,
        "is_default_password": is_default_password(username),
        "role": role,
        "is_lead": role == ROLE_LEAD,
    }


def login_user(user: dict[str, Any], remember: bool = True) -> None:
    """
    写入会话（登录成功 / 修改密码后调用）。

    :param user: 用户字典
    :param remember: 是否长期保持登录
    """
    session.clear()
    session[SESSION_USER_KEY] = user["username"]
    session.permanent = bool(remember)
    database.touch_last_login(user["username"])


def logout_user() -> str:
    """
    清除会话。

    :return: 被登出的登录名（可能为空）
    """
    username = current_username()
    session.clear()
    return username


# ============================================================
# 访问控制装饰器
# ============================================================
def login_required(view: Callable) -> Callable:
    """
    页面装饰器：未登录跳转到登录页，并记住原本要去的地址。

    :param view: 视图函数
    :return: 包装后的视图函数
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not current_username():
            return redirect(url_for("login_page", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def api_login_required(view: Callable) -> Callable:
    """
    API 装饰器：未登录返回 401 JSON（前端据此跳转登录页）。

    :param view: 视图函数
    :return: 包装后的视图函数
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        username = current_username()
        if not username:
            return jsonify({"success": False, "error": "请先登录", "need_login": True}), 401
        return view(*args, **kwargs)

    return wrapper


# ============================================================
# 任务级权限
# ============================================================
def task_owner(task: dict[str, Any]) -> str:
    """
    取任务的负责人登录名。

    :param task: 任务字典
    :return: 负责人代号；未分配时返回空串
    """
    return str(task.get("assignee_username") or task.get("assignee") or "")


def can_edit_task(task: dict[str, Any], username: str) -> bool:
    """
    能否操作该任务（是否可拖拽 / 可否改状态）。

    规则：
      * **组长 → 任何任务都可以**（要能给别人分派、也要能替团队收尾）
      * 负责人是自己 → 可以
      * 任务未分配   → 可以（先去认领）
      * 别人的任务   → 不可以

    :param task: 任务字典
    :param username: 当前登录名
    :return: 是否允许
    """
    if is_lead(username):
        return True
    owner = task_owner(task)
    return (not owner) or owner == username


def check_status_permission(task: dict[str, Any], username: str) -> str:
    """
    校验「改状态」的权限。

    :param task: 任务字典
    :param username: 当前登录名
    :return: 错误信息；空串表示允许
    """
    if is_lead(username):
        return ""            # 组长可以推进任何人的卡片
    owner = task_owner(task)
    if not owner:
        return "这个任务还没有负责人，请先把它指派给自己再开始工作"
    if owner != username:
        return f"这是 {assignee_display(owner)} 的任务，你只能操作自己的任务"
    return ""


def check_assign_permission(
    task: dict[str, Any], username: str, new_assignee: str
) -> str:
    """
    校验「改负责人」的权限。

    策略：

    * **组长可以随便分派**：任何任务 → 任何人（含从别人手里拿走、直接派给某人）
    * 组员（ALLOW_ASSIGN_TO_OTHERS=False 时）：
      - **不能把任务指派给别人**：只能本人自己认领
      - 未分配的任务 → 任何人都可以认领给自己
      - 自己名下的任务 → 可以放回「未分配」，但不能转给第三人
      - 别人的任务 → 一律不许动
    * 若把 ``ALLOW_ASSIGN_TO_OTHERS`` 设为 1，则等于「所有人都是组长」

    :param task: 任务字典
    :param username: 当前登录名
    :param new_assignee: 目标负责人代号；空串表示取消分配
    :return: 错误信息；空串表示允许
    """
    if is_lead(username) or ALLOW_ASSIGN_TO_OTHERS:
        return ""

    owner = task_owner(task)

    # 别人的任务：无论怎么改都不允许
    if owner and owner != username:
        return f"这是 {assignee_display(owner)} 的任务，你只能操作自己的任务"

    # 想指派给别人（既不是自己，也不是「未分配」）→ 拒绝
    if new_assignee and new_assignee != username:
        return (f"不能把任务指派给 {assignee_display(new_assignee)}："
                f"只有本人能认领任务（把它放到「未分配」列，让对方自己认领）")
    return ""


def check_delete_permission(task: dict[str, Any], username: str) -> str:
    """
    校验「从看板移除」的权限。

    :param task: 任务字典
    :param username: 当前登录名
    :return: 错误信息；空串表示允许
    """
    if is_lead(username):
        return ""
    owner = task_owner(task)
    if not owner or owner == username:
        return ""
    return f"这是 {assignee_display(owner)} 的任务，只有本人能删除"


def done_blocked_message() -> str:
    """
    「不能手动完成」的提示文案（前后端共用一份，避免说法不一致）。

    :return: 提示文本
    """
    return "不能手动把任务标成「完成」：请提交代码并等待 Agent 审核，审核通过后会自动移到 Done"


def done_final_message() -> str:
    """
    「完成的任务不能再改动」的提示文案（前后端共用一份）。

    Done 列是 Agent 审核通过的结果，允许从界面上拖出去就等于绕过了审核；
    确实需要返工时，把配置里的 ``DONE_IS_FINAL`` 设为 0。

    :return: 提示文本
    """
    return ("已完成的任务不能再改动：Done 是 Agent 审核通过的结果，"
            "如需返工请让管理员把 DONE_IS_FINAL 设为 0")


# ============================================================
# 命令行：查看用户 / 重置密码
# ============================================================
def _cli(argv: list[str]) -> int:
    """
    简单的命令行工具，管理员在本机用。

    用法::

        python auth.py list                        # 看有哪些人、谁还在用初始密码、谁是组长
        python auth.py reset 小美                   # 把某人密码重置成 config.DEFAULT_PASSWORD
        python auth.py reset 小美 新的密码            # 重置成指定密码
        python auth.py role 小美 lead              # 把某人设为组长（可给任何人分派任务、放行并合并 PR）
        python auth.py role 小美 member            # 取消组长，恢复成普通组员

    :param argv: 命令行参数
    :return: 进程退出码
    """
    import database as _database

    _database.init_db()
    ensure_default_users()

    action = (argv[0] if argv else "list").lower()

    if action == "list":
        print(f"{'登录名':<10}{'显示名':<10}{'角色':<8}{'初始密码':<10}最近登录")
        for user in _database.list_users():
            name = user["username"]
            role = "组长" if (user.get("role") == ROLE_LEAD) else "组员"
            print(f"{name:<10}{assignee_display(name) or name:<10}{role:<8}"
                  f"{'是 ⚠️' if is_default_password(name) else '否':<10}"
                  f"{user.get('last_login_at') or '从未'}")
        return 0

    if action == "role":
        if len(argv) < 3:
            print("用法：python auth.py role <登录名> <lead|member>")
            return 2
        username = resolve_username(argv[1])
        if not username:
            print(f"❌ 找不到用户：{argv[1]}")
            return 1
        role = argv[2].strip().lower()
        if role not in ROLES:
            print(f"❌ 角色只能是 {' / '.join(ROLES)}")
            return 1
        if not _database.set_user_role(username, role):
            print(f"❌ 设置失败：{username}")
            return 1
        _database.log_operation(username, "set_role", new_value=role, detail="命令行设置角色")
        label = "组长" if role == ROLE_LEAD else "组员"
        print(f"✅ {username}（{assignee_display(username) or username}）现在角色是：{label}")
        if role == ROLE_LEAD:
            print("   组长权限：人员视图可以把任意卡片拖给任何人；可以放行并合并 PR")
        return 0

    print(__doc__)
    return 2

    if action == "reset":
        if len(argv) < 2:
            print("用法：python auth.py reset <登录名> [新密码]")
            return 2
        username = resolve_username(argv[1])
        if not username:
            print(f"❌ 找不到用户：{argv[1]}")
            return 1
        new_password = argv[2] if len(argv) > 2 else DEFAULT_PASSWORD
        if len(new_password) < MIN_PASSWORD_LENGTH:
            print(f"❌ 密码至少 {MIN_PASSWORD_LENGTH} 位")
            return 1

        from werkzeug.security import generate_password_hash as _hash

        _database.set_user_password(username, _hash(new_password))
        _database.log_operation(username, "change_password", detail="管理员通过命令行重置")
        print(f"✅ 已把 {username}（{assignee_display(username) or username}）的密码重置为：{new_password}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    _sys.exit(_cli(_sys.argv[1:]))
