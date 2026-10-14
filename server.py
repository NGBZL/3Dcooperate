"""
Flask 后端服务
-------------
为前端看板提供 REST API，并把数据落在本地 SQLite 缓存中。

看板有两个互相独立的人员/进度维度：
    * 状态维度：todo / doing / done（GitHub Issue 标签）
    * 人员维度：固定 6 人 + 未分配（GitHub Issue body 中的 “**负责人**: 姓名”）

接口一览（除 /login、静态资源外，全部需要登录）：
    GET    /login                 登录页
    POST   /login                 提交登录
    GET    /logout                登出
    GET    /                      看板页面
    GET    /api/me                当前登录用户
    POST   /api/change-password   修改密码
    GET    /api/health            健康检查（?deep=1 会真实请求 GitHub）
    GET    /api/server-info       本机局域网访问地址（给组员用）
    GET    /api/assignees         负责人名单与各自任务数
    GET    /api/tasks             读取本地全部任务（支持 ?status= & ?assignee= & ?q= 过滤）
    POST   /api/tasks             新建任务（同时在 GitHub 创建 Issue，负责人默认为自己）
    PUT    /api/tasks/<编号>       更新任务状态/负责人（同步改 GitHub；**不能改成 done**）
    PUT    /api/tasks/<编号>/assignee  仅更新负责人（同步改 GitHub body）
    DELETE /api/tasks/<编号>       从本地缓存删除任务
    POST   /api/sync             从 GitHub 拉取最新 Issue 覆盖本地缓存
    GET    /api/logs              最近的操作日志（审计）

权限规则：
    * 每个人可以查看全部任务
    * 只有「负责人是自己」的任务才能改状态；未分配的任务需要先认领
    * 「完成（done）」只能由 Agent 审核通过后写入，人工设置会被 403 拒绝
"""

from __future__ import annotations

import logging
import re
import socket
from typing import Any, Optional

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

import auth
import database
import github_client
import realtime
from config import (
    ALLOW_ASSIGN_TO_OTHERS,
    ASSIGNEES,
    ASSIGNEE_ALIASES_PATH,
    COMMITS_PER_TASK,
    DB_PATH,
    DEBUG,
    DEFAULT_ASSIGNEE,
    DEFAULT_BRANCH,
    DONE_REQUIRES_AGENT,
    DONE_IS_FINAL,
    GITHUB_REPO,
    GITHUB_TOKEN,
    HOST,
    MIN_PASSWORD_LENGTH,
    PORT,
    PR_BODY_TEMPLATE,
    PR_MERGE_METHOD,
    PR_SYNC_BUDGET,
    SESSION_LIFETIME_HOURS,
    STATUSES,
    STATUS_LABELS,
    SYNC_BRANCH_COMMIT_BUDGET,
    SYNC_ISSUE_STATE,
    SYNC_MAX_PAGES,
    SYNC_REVIEW_BUDGET,
    UNASSIGNED_LABEL,
    assignee_aliases,
    assignee_code,
    assignee_display,
    clean_assignee,
    mask_real_names,
    mask_token,
    normalize_assignee,
    setup_logging,
    validate_config,
)
from github_client import GitHubError, RepoMetaCollector

logger = logging.getLogger(__name__)

app = Flask(__name__)
# 允许跨域：方便前端单独用 Live Server 调试
CORS(app)

# 统一 JSON 输出（中文不转义），便于阅读
app.json.ensure_ascii = False

# 会话与密钥
auth.init_app(app)


# ============================================================
# 通用工具
# ============================================================
def ok(**payload: Any):
    """构造统一格式的成功响应。"""
    body = {"success": True}
    body.update(payload)
    return jsonify(body)


def fail(message: str, code: int = 400, **extra: Any):
    """
    构造统一格式的错误响应。

    :param message: 面向用户的错误说明
    :param code: HTTP 状态码
    """
    body = {"success": False, "error": message}
    body.update(extra)
    return jsonify(body), code


def _parse_json_body() -> dict[str, Any]:
    """安全地读取 JSON 请求体，非法 JSON 时当作空字典处理。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalize_status(value: Any, default: str = "todo") -> str:
    """把任意输入规整成合法状态；非法值返回 default。"""
    status = str(value or "").strip().lower()
    return status if status in STATUSES else default


def _me() -> str:
    """取当前登录名（装饰器保证已登录，这里只是少写点字）。"""
    return auth.current_username()


def _log_denied(action: str, issue_number: Optional[int], reason: str) -> None:
    """
    记一条「被拒绝」的操作日志，便于事后审计。

    日志里会把真名换成代号：界面提示要给人看（显示真名），
    但日志/数据库只保留代号，避免真名扩散。
    """
    safe_reason = mask_real_names(reason)
    database.log_operation(_me(), action, issue_number=issue_number,
                           result="denied", detail=safe_reason)
    logger.warning("拒绝 %s 的操作：%s", _me(), safe_reason)


def _deny(message: str, action: str, issue_number: Optional[int] = None,
          code: int = 403, **extra: Any):
    """
    统一的拒绝响应：记日志 + 返回 JSON。

    :param message: 给用户看的错误信息
    :param action: 动作名（写进日志）
    :param issue_number: 相关 Issue 编号
    :param code: HTTP 状态码
    """
    _log_denied(action, issue_number, message)
    return fail(message, code, **extra)


def _task_with_permission(issue_number: int, check, action: str, **check_kwargs: Any):
    """
    取任务并做权限校验的公共流程。

    :param issue_number: Issue 编号
    :param check: 校验函数（auth.check_*）
    :param action: 动作名（用于日志）
    :param check_kwargs: 传给校验函数的额外参数
    :return: (task, error_response)；error_response 为 None 表示通过
    """
    task = database.get_task_by_issue(issue_number)
    if not task:
        return None, fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)

    message = check(task, _me(), **check_kwargs)
    if message:
        return task, _deny(message, action, issue_number)
    return task, None


def _parse_assignee_input(value: Any) -> tuple[Optional[str], Optional[str]]:
    """
    解析并校验前端传来的负责人，统一折算成**代号**。

    允许的写法：代号（小明…）、本地真名、以及“未分配”/""/none/- 等别名。
    无论前端传哪一种，落到数据库与 GitHub 上的都只会是代号。

    :param value: 原始输入
    :return: (代号, 错误信息)；错误信息不为 None 时应返回 400
    """
    if value is None:
        return None, None

    raw = clean_assignee(str(value))
    code = assignee_code(raw)

    if code is not None:
        return code, None

    # 其余情况说明是名单外的人名，明确报错而不是静默改成未分配
    allowed = "、".join(assignee_display(name) for name in ASSIGNEES)
    return None, f"无效的负责人：{raw}（只能是 {allowed} 或 {UNASSIGNED_LABEL}）"


# ============================================================
# 局域网访问地址
# ============================================================
def get_local_ip() -> str:
    """
    获取本机用于对外通信的局域网 IP（例如连热点时的 192.168.43.144）。

    原理：UDP socket ``connect`` 到公网地址**不会真的发包**，只是让系统按路由表
    选出出口网卡，从而拿到该网卡的地址。拿不到时退回 127.0.0.1。

    :return: IPv4 地址字符串
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.5)
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def list_local_ips() -> list[str]:
    """
    列出本机所有可用于局域网访问的 IPv4 地址。

    会把「默认路由出口」的那个地址排在第一位（就是组员实际能连上的那个），
    其余网卡地址（VPN、虚拟网卡、有线+无线同时插着等）作为备选一并返回，
    方便第一条连不通时换一个试。

    :return: 去重后的地址列表；没有任何可用地址时返回空列表
    """
    ordered: list[str] = []

    primary = get_local_ip()
    if primary:
        ordered.append(primary)

    # 主机名解析出的地址（多网卡时能补齐 UDP 探测拿不到的那些）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in ordered:
                ordered.append(address)
    except OSError:
        pass

    def usable(ip: str) -> bool:
        """排除回环、链路本地（169.254.x.x，通常是没拿到 DHCP）与无效地址。"""
        return bool(ip) and not ip.startswith(("127.", "169.254.", "0."))

    return [ip for ip in ordered if usable(ip)]


def build_server_info() -> dict[str, Any]:
    """
    组装「怎么访问这个看板」的信息。

    :return: 含 ip / port / url / urls / hostname 等字段的字典
    """
    addresses = list_local_ips()
    hostname = socket.gethostname()

    urls = [f"http://{ip}:{PORT}" for ip in addresses]
    primary_url = urls[0] if urls else f"http://127.0.0.1:{PORT}"

    return {
        "ip": addresses[0] if addresses else "127.0.0.1",
        "port": PORT,
        "url": primary_url,
        # 备选地址：第一条连不上时（多网卡/VPN 挑错网卡）可以换着试
        "urls": urls,
        "hostname": hostname,
        "hostname_url": f"http://{hostname}:{PORT}",
        # True 表示没探测到任何局域网地址（没连热点/Wi-Fi，或没有默认路由）
        "local_only": not addresses,
        # 其它设备连不上时最常见的原因，前端会用 ⓘ 按钮展示
        "firewall_hint": (
            "其它设备打不开时，通常是本机 Windows 防火墙拦住了。"
            f"用管理员 PowerShell 执行一次即可放行："
            f'New-NetFirewallRule -DisplayName "Kanban {PORT}" -Direction Inbound '
            f"-Protocol TCP -LocalPort {PORT} -Action Allow"
        ),
    }


# ============================================================
# 登录 / 登出
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login_page():
    """
    登录页（GET）与登录提交（POST）。

    表单字段：username（代号或真名都可以）、password、next（登录后跳回哪一页）。
    登录失败时把错误信息回填到页面上，不泄露「用户是否存在」。
    """
    if request.method == "GET":
        if auth.current_username():
            return redirect(url_for("index"))
        return render_template("login.html", error="", username="",
                               next_url=request.args.get("next", "") or "")

    form = request.form
    raw_username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_url = (form.get("next") or "").strip()

    user, error = auth.verify_login(raw_username, password)
    if error:
        return render_template("login.html", error=error, username=raw_username,
                               next_url=next_url), 401

    auth.login_user(user, remember=True)
    # 日志里只留代号：真名不打进数据库
    logger.info("用户 %s 登录成功", user["username"])
    database.log_operation(user["username"], "login", detail="登录成功")

    # 只允许跳回本站内部地址，避免被构造成外站跳转
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("index"))


@app.route("/logout", methods=["GET", "POST"])
def logout():
    """登出并回到登录页。"""
    username = auth.logout_user()
    if username:
        logger.info("用户 %s 已登出", username)
        database.log_operation(username, "logout")
    return redirect(url_for("login_page"))


@app.route("/api/me", methods=["GET"])
@auth.api_login_required
def api_me():
    """
    当前登录用户信息（前端用它决定显示与权限）。

    :return: {username, display_name, is_default_password, min_password_length,
              done_requires_agent}
    """
    user = auth.current_user()
    if not user:
        return fail("请先登录", 401)

    info = auth.user_public_info(user)
    is_lead = auth.is_lead(user["username"])
    info.update({
        "min_password_length": MIN_PASSWORD_LENGTH,
        "done_requires_agent": DONE_REQUIRES_AGENT,
        # 前端据此决定「完成的任务能不能拖出去」
        "done_is_final": DONE_IS_FINAL,
        # 前端据此决定「负责人」下拉框 / 拖拽能不能选别人：
        # 组长可以随便分派；组员按 ALLOW_ASSIGN_TO_OTHERS（默认只能领取）
        "allow_assign_to_others": is_lead or ALLOW_ASSIGN_TO_OTHERS,
        "is_lead": is_lead,
        "can_merge_pr": is_lead,
    })
    return jsonify(info)


@app.route("/api/change-password", methods=["POST"])
@auth.api_login_required
def api_change_password():
    """
    修改密码。

    请求体：{"old_password": "...", "new_password": "..."}
    """
    data = _parse_json_body()
    old_password = str(data.get("old_password") or "")
    new_password = str(data.get("new_password") or "")
    username = _me()

    error = auth.change_password(username, old_password, new_password)
    if error:
        database.log_operation(username, "change_password", result="denied", detail=error)
        return fail(error, 400)

    database.log_operation(username, "change_password", detail="密码已更新")
    logger.info("用户 %s 修改了密码", username)
    return ok(message="密码已更新，下次请用新密码登录")


@app.route("/api/logs", methods=["GET"])
@auth.api_login_required
def api_logs():
    """
    最近的操作日志（审计用）。

    :query limit: 最多返回多少条（默认 50，上限 1000）
    :query mine: 传 1 只看自己的操作
    """
    limit = request.args.get("limit", type=int) or 50
    mine = str(request.args.get("mine", "")).lower() in ("1", "true", "yes")
    logs = database.recent_logs(limit=limit, username=_me() if mine else None)
    return jsonify(logs)


# ============================================================
# 页面
# ============================================================
@app.route("/")
@auth.login_required
def index():
    """渲染看板页面（未登录会跳转到登录页）。"""
    return render_template("index.html")


# ============================================================
# 健康检查 / 元信息
# ============================================================
@app.route("/api/server-info", methods=["GET"])
def server_info():
    """
    返回本机的局域网访问地址，供页面提示组员。

    这个接口**故意不需要登录**：它只暴露局域网地址与防火墙提示，
    组员在登录页就能看到「该访问哪个地址」，不涉及任何任务数据。

    :return: {"ip", "port", "url", "urls", "hostname", "hostname_url",
              "local_only", "firewall_hint"}
    """
    try:
        return jsonify(build_server_info())
    except Exception as exc:  # 兜底：不能让这个小接口把页面搞挂
        logger.exception("获取本机地址失败")
        return fail(f"无法获取访问地址：{exc}", 500)


@app.route("/api/health", methods=["GET"])
@auth.api_login_required
def health():
    """
    健康检查。

    :query deep: 传 1/true 时会真实调用 GitHub 校验 Token 与仓库权限
    """
    deep = str(request.args.get("deep", "")).lower() in ("1", "true", "yes")

    payload: dict[str, Any] = {
        "status": "ok",
        "repo": GITHUB_REPO or None,
        "token": mask_token() or None,
        "token_configured": bool(GITHUB_TOKEN),
        "repo_configured": bool(GITHUB_REPO and "/" in GITHUB_REPO),
        "db_path": DB_PATH,
        "sync_state": SYNC_ISSUE_STATE,
        "assignees": list(ASSIGNEES),
        "aliases_configured": bool(assignee_aliases()),
        "unassigned_label": UNASSIGNED_LABEL,
        "counts": database.count_by_status(),
        "assignee_counts": database.count_by_assignee(),
        "config_errors": validate_config(),
        "realtime": realtime.HUB.stats(),
    }

    if deep:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            payload["github"] = {"ok": False, "message": "Token 或仓库未配置，无法探测 GitHub"}
            payload["status"] = "degraded"
        else:
            try:
                payload["github"] = {"ok": True, **github_client.validate_token()}
            except GitHubError as exc:
                payload["status"] = "degraded"
                payload["github"] = {"ok": False, "message": exc.message}

    return jsonify(payload)


@app.route("/api/assignees", methods=["GET"])
@auth.api_login_required
def get_assignees():
    """
    返回负责人代号名单、代号到真名的本地映射，以及各自的任务数量。

    前端用 display_names 把代号翻译成真名显示；GitHub 侧永远只用代号。

    :return: {"assignees": [...], "display_names": {...},
              "unassigned_label": "未分配", "counts": {...}}
    """
    try:
        counts = database.count_by_assignee()
    except Exception as exc:
        logger.exception("统计负责人任务数失败")
        return fail(f"读取本地数据库失败：{exc}", 500)

    aliases = assignee_aliases()
    # 只回传代号 -> 真名，未配置映射的代号由前端原样显示
    display_names = {code: aliases.get(code, code) for code in ASSIGNEES}

    return jsonify({
        "success": True,
        "assignees": list(ASSIGNEES),
        "display_names": display_names,
        "aliases_configured": bool(aliases),
        "aliases_path": ASSIGNEE_ALIASES_PATH,
        "unassigned_label": UNASSIGNED_LABEL,
        "counts": counts,
    })


# ============================================================
# 实时通道（WebSocket）
# ============================================================
@app.route("/ws", websocket=True)
def websocket_endpoint():
    """
    看板的实时通道：推送「任务有变化」与在线名单。

    只推事件、不推状态：客户端收到 ``tasks_changed`` 后自己重新拉一次
    ``/api/tasks``（任务真值永远在 SQLite + GitHub 里，避免两份状态打架）。

    权限：**在升级协议之前**用 session 校验，未登录直接返回 401，
    不会「先建连接再踢人」。所有写操作依然只能走带权限校验的 HTTP 接口。

    注意 ``websocket=True``：握手请求带 ``Connection: Upgrade`` +
    ``Upgrade: websocket``，Werkzeug 会把它的 url_scheme 认成 ``ws``，
    只匹配声明了 ``websocket=True`` 的路由；不写这个参数会在**进入视图之前**
    就抛 WebsocketMismatch（表现为 400/500，连接建不起来）。

    :return: WebSocket 关闭后返回空响应（连接期间由 simple-websocket 直接读写原始 socket）
    """
    if not realtime.WEBSOCKET_AVAILABLE:
        return fail("服务端未安装 simple-websocket，实时通道不可用（pip install simple-websocket）", 503)

    user = auth.current_user()
    if not user:
        # 还没握手，可以用普通的 401 JSON 拒绝
        return fail("请先登录", 401)

    try:
        ws = realtime.WebSocketServer(request.environ, ping_interval=realtime.PING_INTERVAL)
    except Exception as exc:                          # noqa: BLE001 - 拿不到原始 socket 等
        logger.warning("WebSocket 握手失败：%s", exc)
        return fail("WebSocket 握手失败", 400)

    client = realtime.HUB.register(
        ws,
        user["username"],
        user.get("display_name") or "",
        request.remote_addr or "",
    )

    try:
        realtime.HUB.send_to(client, realtime.HUB.hello_payload(client))
        realtime.HUB.broadcast_presence()             # 让所有人知道谁上线了

        while True:
            message = ws.receive(timeout=realtime.RECEIVE_TIMEOUT)
            if message is None:
                # 只是这段时间没说话，不算断开（协议层 ping 一直在跑）
                continue
            realtime.HUB.handle_message(client, message)
    except realtime.ConnectionClosed:
        pass
    except Exception as exc:                          # noqa: BLE001
        logger.warning("实时连接 #%s 异常结束：%s", client.id, exc)
    finally:
        realtime.HUB.unregister(client)
        realtime.HUB.broadcast_presence()             # 让所有人知道谁下线了

    client.close()
    return "", 204


# ============================================================
# 任务读取
# ============================================================
@app.route("/api/tasks", methods=["GET"])
@auth.api_login_required
def get_tasks():
    """
    获取本地缓存的全部任务（所有人都能看全部任务）。

    :query status: 可选，仅返回某个状态（todo/doing/done）
    :query assignee: 可选，仅返回某负责人的任务（代号或本地真名都支持，未分配传空串）
    :query mine: 可选，传 1 只返回「负责人是自己」的任务
    :query include_hidden: 可选，传 1 连「已从看板移除」的任务一起返回
    :query q: 可选，标题/描述/编号/负责人（代号或真名）的模糊搜索关键字

    每条任务都会带上 ``can_edit``：前端据此把别人的卡片置灰、禁止拖拽。
    """
    include_hidden = str(request.args.get("include_hidden", "")).lower() in ("1", "true", "yes")
    try:
        tasks = database.get_all_tasks(include_hidden=include_hidden)
    except Exception as exc:  # 数据库异常
        logger.exception("读取任务列表失败")
        return fail(f"读取本地数据库失败：{exc}", 500)

    status_filter = str(request.args.get("status", "")).strip().lower()
    if status_filter in STATUSES:
        tasks = [task for task in tasks if task.get("status") == status_filter]

    if "assignee" in request.args:
        raw = request.args.get("assignee", "")
        # 真名会被折算成代号，因此 ?assignee=真名 也能正确过滤
        wanted = normalize_assignee(raw) if clean_assignee(raw) else ""
        tasks = [task for task in tasks if (task.get("assignee") or "") == wanted]

    if str(request.args.get("mine", "")).lower() in ("1", "true", "yes"):
        me = _me()
        tasks = [task for task in tasks if auth.task_owner(task) == me]

    keyword = str(request.args.get("q", "")).strip().lower()
    if keyword:
        tasks = [
            task for task in tasks
            if keyword in (task.get("title") or "").lower()
            or keyword in (task.get("description") or "").lower()
            or keyword in str(task.get("issue_number") or "")
            or keyword in (task.get("assignee") or "").lower()
            or keyword in assignee_display(task.get("assignee") or "").lower()
        ]

    # 给前端算好权限，避免前后端各写一套规则导致不一致
    me = _me()
    for task in tasks:
        task["can_edit"] = auth.can_edit_task(task, me)
        task["is_mine"] = auth.task_owner(task) == me
        task["assignee_name"] = assignee_display(task.get("assignee") or "")

    return jsonify(tasks)


# ============================================================
# 新建任务
# ============================================================
@app.route("/api/tasks", methods=["POST"])
@auth.api_login_required
def create_task():
    """
    新建任务：先建 GitHub Issue，再写入本地缓存。

    请求体：{"title": "...", "description": "...", "status": "todo", "assignee": "小明"}
    不传 assignee 时，负责人默认为**创建者自己**。
    """
    data = _parse_json_body()
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    status = _normalize_status(data.get("status"), "todo")

    # 负责人默认是自己（需求：创建 Issue 时负责人默认自己）
    if data.get("assignee") is None:
        assignee: Optional[str] = _me()
    else:
        assignee, assignee_error = _parse_assignee_input(data.get("assignee"))
        if assignee_error:
            return fail(assignee_error, 400)

    # 不允许「替别人建任务」：只有组长（或显式放开 ALLOW_ASSIGN_TO_OTHERS）能直接派活
    if (assignee and assignee != _me()
            and not ALLOW_ASSIGN_TO_OTHERS and not auth.is_lead(_me())):
        return _deny(
            f"不能把新任务直接派给 {assignee_display(assignee)}："
            f"只能建给自己的任务，或选「未分配」让本人认领（组长可以直接派）",
            "create_task",
        )

    if not title:
        return fail("标题不能为空", 400)
    if len(title) > 200:
        return fail("标题过长（最多 200 个字符）", 400)

    # 新建任务时不允许直接建到 done（完成只能由 Agent 写）
    if status == "done" and DONE_REQUIRES_AGENT:
        return _deny(auth.done_blocked_message(), "create_task")

    try:
        # 首次使用时自动创建 todo/doing/done 标签（已存在则忽略）
        github_client.ensure_status_labels()

        issue = github_client.create_issue(
            title=title,
            # 负责人写进 body，格式：**负责人**: 小明
            body=github_client.build_issue_body(description, assignee or ""),
            labels=[STATUS_LABELS[status]],
        )
        task = database.upsert_task(
            issue_number=issue["number"],
            title=issue.get("title", title),
            description=description,
            status=status,
            assignee=assignee or "",
            html_url=issue.get("html_url", ""),
            state=issue.get("state", "open"),
        )
    except GitHubError as exc:
        logger.error("创建 Issue 失败：%s", exc.message)
        database.log_operation(_me(), "create_task", result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)
    except Exception as exc:
        logger.exception("创建任务失败")
        return fail(f"创建任务失败：{exc}", 500)

    database.log_operation(_me(), "create_task", issue_number=issue["number"],
                           new_value=f"status={status}, assignee={assignee or UNASSIGNED_LABEL}",
                           detail=title[:80])

    # 日志里只写代号，界面提示里写真名（真名映射只存在于本地）
    logger.info("任务创建成功：#%s %s（负责人代号：%s）",
                issue["number"], title, assignee or UNASSIGNED_LABEL)
    # 实时通知其他人：看板变了（事件里只有代号，真名由前端翻译）
    realtime.notify_tasks_changed("create_task", issue=issue["number"], by=_me(),
                                  detail=f"新建：{title[:40]}")
    return ok(task=task, issue_number=issue["number"],
              message=f"Issue #{issue['number']} 创建成功"
                      f"（负责人：{assignee_display(assignee) or UNASSIGNED_LABEL}）"), 201


# ============================================================
# 更新任务（状态 / 负责人）
# ============================================================
def _apply_assignee(issue_number: int, assignee: str) -> None:
    """
    把负责人变更同步到 GitHub 与本地库（先改 GitHub，成功后再落库）。

    改的是 Issue body 里的负责人标记行；会先拉取 GitHub 上的最新正文再改写，
    因此不会覆盖别人在网页上对正文做的修改。

    :param issue_number: Issue 编号
    :param assignee: 新负责人；空串表示未分配
    :raises GitHubError: GitHub 侧更新失败
    """
    github_client.set_issue_assignee(issue_number, assignee)
    database.update_task_assignee(issue_number, assignee)


@app.route("/api/tasks/<int:issue_number>", methods=["PUT", "PATCH"])
@auth.api_login_required
def update_task(issue_number: int):
    """
    更新任务：可只改状态、只改负责人，或两者一起改。

    请求体：{"status": "doing"} / {"assignee": "小明"} / 两者同时传

    权限与安全规则：
      * 只有「负责人是自己」的任务才能改状态；未分配的任务要先认领
      * **不能把状态改成 done**：完成只能由 Agent 审核通过后写入
      * 改负责人遵循 auth.check_assign_permission（未分配的任务谁都能指派）
    """
    data = _parse_json_body()

    # ---- 参数校验 ----
    has_status = "status" in data and data.get("status") is not None
    has_assignee = "assignee" in data

    if not has_status and not has_assignee:
        return fail("请至少提供 status 或 assignee 字段", 400)

    new_status: Optional[str] = None
    if has_status:
        raw_status = str(data.get("status", "")).strip().lower()
        if raw_status not in STATUSES:
            return fail(f"无效状态：{raw_status or '空'}（只能是 {'/'.join(STATUSES)}）", 400)
        new_status = raw_status

    new_assignee: Optional[str] = None
    if has_assignee:
        new_assignee, assignee_error = _parse_assignee_input(data.get("assignee"))
        if assignee_error:
            return fail(assignee_error, 400)

    task = database.get_task_by_issue(issue_number)
    if not task:
        return fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)

    # ---- 防「假完成」：人工不能把状态改成 done ----
    if new_status == "done" and DONE_REQUIRES_AGENT:
        return _deny(auth.done_blocked_message(), "update_status", issue_number,
                     current_status=task.get("status"))

    # ---- 完成是终态：已 done 的任务不能再被移出去（否则等于绕过 Agent 审核）----
    if (new_status is not None and new_status != "done"
            and task.get("status") == "done" and DONE_IS_FINAL):
        return _deny(auth.done_final_message(), "update_status", issue_number,
                     current_status="done")

    # ---- 权限：改状态要求是负责人本人；改负责人按认领规则 ----
    # 例外（有意为之）：未分配的任务，只要这次请求**同时**把它认领给自己，
    # 就允许一起改状态——这就是「拖到自己的区域即认领并推进」，省掉「先认领再拖」两步。
    claiming_to_self = (
        new_assignee is not None
        and new_assignee == _me()
        and not (task.get("assignee") or "")
    )
    if (new_status is not None and new_status != task.get("status")
            and not claiming_to_self):
        message = auth.check_status_permission(task, _me())
        if message:
            return _deny(message, "update_status", issue_number)

    if new_assignee is not None and new_assignee != (task.get("assignee") or ""):
        message = auth.check_assign_permission(task, _me(), new_assignee)
        if message:
            return _deny(message, "update_assignee", issue_number)

    # ---- 先改 GitHub，再改本地 ----
    try:
        if new_assignee is not None and new_assignee != (task.get("assignee") or ""):
            _apply_assignee(issue_number, new_assignee)

        if new_status is not None and new_status != task.get("status"):
            github_client.ensure_status_labels()
            github_client.set_issue_status(issue_number, new_status)
            database.update_task_status(issue_number, new_status)
    except GitHubError as exc:
        logger.error("更新 Issue #%s 失败：%s", issue_number, exc.message)
        database.log_operation(_me(), "update_task", issue_number=issue_number,
                               result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)
    except Exception as exc:
        logger.exception("更新任务 #%s 失败", issue_number)
        return fail(f"更新任务失败：{exc}", 500)

    parts = []
    if new_status is not None and new_status != task.get("status"):
        parts.append(f"状态={new_status}")
        database.log_operation(_me(), "update_status", issue_number=issue_number,
                               old_value=task.get("status"), new_value=new_status)
    if new_assignee is not None and new_assignee != (task.get("assignee") or ""):
        # 界面提示用真名；日志仍然只记代号
        parts.append(f"负责人={assignee_display(new_assignee) or UNASSIGNED_LABEL}")
        database.log_operation(_me(), "update_assignee", issue_number=issue_number,
                               old_value=task.get("assignee") or UNASSIGNED_LABEL,
                               new_value=new_assignee or UNASSIGNED_LABEL)
    detail = "，".join(parts) or "无实际变化"

    logger.info("任务 #%s 已更新（状态=%s 负责人代号=%s）",
                issue_number, new_status, new_assignee)
    if parts:
        realtime.notify_tasks_changed("update_task", issue=issue_number, by=_me(),
                                      detail="，".join(parts))
    return ok(issue_number=issue_number, status=new_status, assignee=new_assignee,
              message=f"#{issue_number} 已更新（{detail}）")


@app.route("/api/tasks/<int:issue_number>/assignee", methods=["PUT", "PATCH"])
@auth.api_login_required
def update_task_assignee(issue_number: int):
    """
    仅更新负责人（人员视图拖拽 / 点击徽章切换时调用）。

    请求体：{"assignee": "小明"}；传 "未分配" 或 "" 表示取消分配。
    """
    data = _parse_json_body()

    if "assignee" not in data:
        return fail("缺少 assignee 字段", 400)

    assignee, assignee_error = _parse_assignee_input(data.get("assignee"))
    if assignee_error:
        return fail(assignee_error, 400)
    assert assignee is not None  # 上面已校验

    task, denied = _task_with_permission(
        issue_number, auth.check_assign_permission, "update_assignee",
        new_assignee=assignee,
    )
    if denied:
        return denied
    assert task is not None

    old_assignee = task.get("assignee") or ""
    if assignee == old_assignee:
        return ok(issue_number=issue_number, assignee=assignee,
                  message=f"#{issue_number} 负责人本来就是 "
                          f"{assignee_display(assignee) or UNASSIGNED_LABEL}")

    try:
        _apply_assignee(issue_number, assignee)
    except GitHubError as exc:
        logger.error("更新 Issue #%s 负责人失败：%s", issue_number, exc.message)
        database.log_operation(_me(), "update_assignee", issue_number=issue_number,
                               result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)
    except Exception as exc:
        logger.exception("更新任务 #%s 负责人失败", issue_number)
        return fail(f"更新负责人失败：{exc}", 500)

    database.log_operation(_me(), "update_assignee", issue_number=issue_number,
                           old_value=old_assignee or UNASSIGNED_LABEL,
                           new_value=assignee or UNASSIGNED_LABEL)
    logger.info("任务 #%s 的负责人代号已改为 %s", issue_number, assignee or UNASSIGNED_LABEL)
    realtime.notify_tasks_changed("update_assignee", issue=issue_number, by=_me(),
                                  detail=f"负责人={assignee or UNASSIGNED_LABEL}")
    return ok(issue_number=issue_number, assignee=assignee,
              message=f"#{issue_number} 负责人已改为 "
                      f"{assignee_display(assignee) or UNASSIGNED_LABEL}")


# ============================================================
# 删除任务（仅本地）
# ============================================================
@app.route("/api/tasks/<int:issue_number>", methods=["DELETE"])
@auth.api_login_required
def delete_task(issue_number: int):
    """
    把任务**从看板移除**（不是删除 GitHub Issue）。

    实现上是把本地记录标记为 hidden：这样同步也**不会**把它拉回来
    （以前是真删本地记录，下一次同步又会出现，看起来就像“我的改动被改回去了”）。
    用 POST /api/tasks/<编号>/restore 可以恢复显示。
    """
    task, denied = _task_with_permission(
        issue_number, auth.check_delete_permission, "hide_task")
    if denied:
        return denied
    assert task is not None

    try:
        hidden = database.hide_task(issue_number, True)
    except Exception as exc:
        logger.exception("移除任务 #%s 失败", issue_number)
        return fail(f"移除任务失败：{exc}", 500)

    if not hidden:
        return fail(f"本地不存在任务 #{issue_number}", 404)

    database.log_operation(_me(), "hide_task", issue_number=issue_number,
                           old_value=task.get("title", ""))
    logger.info("已把任务 #%s 从看板移除（GitHub Issue 不受影响）", issue_number)
    realtime.notify_tasks_changed("hide_task", issue=issue_number, by=_me(),
                                  detail="已从看板移除")
    return ok(message=f"已把 #{issue_number} 从看板移除（GitHub 上的 Issue 没动，可随时恢复）",
              hidden_count=database.count_hidden())


@app.route("/api/tasks/<int:issue_number>/restore", methods=["POST", "PUT"])
@auth.api_login_required
def restore_task(issue_number: int):
    """
    把「已从看板移除」的任务恢复显示。

    :return: {"success": true, "hidden_count": 剩余隐藏数量}
    """
    task = database.get_task_by_issue(issue_number)
    if not task:
        return fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)

    database.hide_task(issue_number, False)
    database.log_operation(_me(), "restore_task", issue_number=issue_number,
                           new_value=task.get("title", ""))
    realtime.notify_tasks_changed("restore_task", issue=issue_number, by=_me(),
                                  detail="已恢复显示")
    return ok(message=f"已恢复 #{issue_number} 的显示", hidden_count=database.count_hidden())


# ============================================================
# 分支 / Pull Request
# ============================================================
def _branch_name_for(task: dict[str, Any]) -> str:
    """
    默认分支名：``task/<编号>-<英文 slug>``。

    标题里的中文不往分支名里塞（URL / 命令行都不方便），只取 ASCII 字母数字；
    取不到就退化成 ``task/<编号>``。

    :param task: 任务字典
    :return: 分支名
    """
    title = str(task.get("title") or "")
    slug = re.sub(r"[^0-9A-Za-z]+", "-", title).strip("-").lower()[:24].strip("-")
    return f"task/{task['issue_number']}-{slug}" if slug else f"task/{task['issue_number']}"


def _task_for_pr(issue_number: int, action: str):
    """
    取任务并校验「建分支 / 提 PR」的权限：**任务负责人本人**或**组长**。

    :return: (task, None) 或 (None, 错误响应)
    """
    task = database.get_task_by_issue(issue_number)
    if not task:
        return None, fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)

    me = _me()
    owner = task.get("assignee") or ""
    if auth.is_lead(me) or (owner and owner == me):
        return task, None

    reason = (f"这是 {assignee_display(owner)} 的任务，只有负责人本人或组长能建分支 / 提 PR"
              if owner else "这个任务还没有负责人：请先认领给自己，或让组长来操作")
    return None, _deny(reason, action, issue_number)


@app.route("/api/tasks/<int:issue_number>/branch", methods=["POST"])
@auth.api_login_required
def create_task_branch(issue_number: int):
    """
    给任务建一个分支（默认 ``task/<编号>-<slug>``，也可以在请求体里指定 name）。

    请求体（都可选）：{"name": "task/12-fix", "from": "main"}

    权限：任务负责人本人，或组长。
    """
    task, denied = _task_for_pr(issue_number, "create_branch")
    if denied:
        return denied
    assert task is not None

    data = _parse_json_body()
    name = str(data.get("name") or "").strip() or _branch_name_for(task)
    from_branch = str(data.get("from") or "").strip() or DEFAULT_BRANCH

    try:
        result = github_client.create_branch(name, from_branch)
    except GitHubError as exc:
        logger.error("给任务 #%s 建分支失败：%s", issue_number, exc.message)
        database.log_operation(_me(), "create_branch", issue_number=issue_number,
                               result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)

    database.update_task_meta(issue_number, branch=result["name"])
    database.log_operation(_me(), "create_branch", issue_number=issue_number,
                           new_value=result["name"])
    realtime.notify_tasks_changed("create_branch", issue=issue_number, by=_me(),
                                  detail=f"分支 {result['name']}")
    return ok(branch=result["name"], created=result["created"], sha=result.get("sha", ""),
              message=(f"已创建分支 {result['name']}（基于 {from_branch}）"
                       if result["created"] else f"分支 {result['name']} 已存在，直接用它"))


@app.route("/api/tasks/<int:issue_number>/pr", methods=["POST"])
@auth.api_login_required
def create_task_pr(issue_number: int):
    """
    给任务提一个 Pull Request。

    请求体（都可选）：{"head": "task/12-fix", "base": "main", "title": "...", "draft": false}

    不传 head 时用卡片上记录的分支（或默认分支名）。PR 正文里会自动写上 ``Closes #编号``，
    这样**合并时 GitHub 会自动关闭 Issue**，看板同步后卡片才进 Done。

    权限：任务负责人本人，或组长。
    """
    task, denied = _task_for_pr(issue_number, "create_pr")
    if denied:
        return denied
    assert task is not None

    data = _parse_json_body()
    head = str(data.get("head") or task.get("branch") or "").strip() or _branch_name_for(task)
    base = str(data.get("base") or "").strip() or DEFAULT_BRANCH
    title = str(data.get("title") or "").strip() or f"{task.get('title')} (#{issue_number})"
    body = str(data.get("body") or PR_BODY_TEMPLATE.format(issue=issue_number))

    try:
        pr = github_client.create_pull_request(head, base, title, body,
                                               draft=bool(data.get("draft")))
    except GitHubError as exc:
        logger.error("给任务 #%s 提 PR 失败：%s", issue_number, exc.message)
        database.log_operation(_me(), "create_pr", issue_number=issue_number,
                               result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)

    database.update_task_pr(issue_number, pr_number=pr["number"], pr_state=pr["state"],
                            pr_url=pr["url"], pr_head=pr.get("head") or head,
                            pr_allowed=0, pr_allowed_by="")
    database.update_task_meta(issue_number, branch=pr.get("head") or head)
    database.log_operation(_me(), "create_pr", issue_number=issue_number,
                           new_value=f"PR #{pr['number']} {pr.get('head')} → {pr.get('base')}")
    realtime.notify_tasks_changed("create_pr", issue=issue_number, by=_me(),
                                  detail=f"PR #{pr['number']}（{pr['state']}）")
    return ok(pr=pr, message=(f"已提交 PR #{pr['number']}（合并后任务才会进 Done）"
                              if pr.get("created", True)
                              else f"这个分支已经有 PR #{pr['number']} 了"))


@app.route("/api/tasks/<int:issue_number>/pr/allow", methods=["POST"])
@auth.api_login_required
def allow_task_pr(issue_number: int):
    """
    **组长放行 PR**：放行之后这个 PR 才允许被合并。

    组员不能调用（403）；组长自己的 PR 也可以直接放行，规则只有一条：
    合并前必须处于「已放行」状态。
    """
    message = auth.check_lead(_me())
    if message:
        return _deny(auth.lead_only_message("「放行 PR」"), "allow_pr", issue_number)

    task = database.get_task_by_issue(issue_number)
    if not task:
        return fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)
    pr_number = task.get("pr_number")
    if not pr_number:
        return fail(f"#{issue_number} 还没有 PR，先提一个", 400)

    database.update_task_pr(issue_number, pr_allowed=1, pr_allowed_by=_me())
    note = "已在 PR 下留言" 
    try:
        github_client.post_issue_comment(
            int(pr_number),
            f"✅ **组长已放行**（看板操作，任务 #{issue_number}）。可以合并了。",
        )
    except GitHubError as exc:
        note = f"PR 留言失败（不影响放行）：{exc.message}"
        logger.warning("放行 PR #%s 时留言失败：%s", pr_number, exc.message)

    database.log_operation(_me(), "allow_pr", issue_number=issue_number,
                           new_value=f"PR #{pr_number}")
    realtime.notify_tasks_changed("allow_pr", issue=issue_number, by=_me(),
                                  detail=f"已放行 PR #{pr_number}")
    return ok(pr_number=pr_number, allowed=True,
              message=f"已放行 PR #{pr_number}，现在可以合并了。（{note}）")


def _latest_agent_verdict(issue_number: int) -> Optional[dict[str, Any]]:
    """
    现读一次 Issue 评论里最新的 Agent 审核报告。

    合并前的闸门要用**最新**结论，不能依赖本地缓存（可能还没同步过）。

    :param issue_number: Issue 编号
    :return: 审核结果字典；没有报告时返回 None
    """
    try:
        comments = github_client.get_issue_comments(issue_number)
    except GitHubError as exc:
        logger.warning("读取 Issue #%s 的审核评论失败：%s", issue_number, exc.message)
        return None
    return github_client.parse_review_from_comments(comments)


def _finalize_merged_issue(issue_number: int, pr_number: Any, *, why: str) -> bool:
    """
    合并完成后的收尾：把 Issue 标成 done 并关闭。

    为什么要看板亲自做这件事：

    * PR 正文里的 ``Closes #编号`` 只在「PR 是看板建的」时才存在，
      组员直接在 GitHub 上提的 PR 合并后 Issue 还开着；
    * Agent 在 PR 事件里是**只审核不定案**的，也不会去打 done 标签；
    * 合并到主干后那次 push 事件里，提交信息往往是 ``标题 (#PR编号)``，
      没有 Issue 编号，Agent 同样不会定案。

    所以「合并」这个动作本身就由看板补齐最后一步：标签 done + 关闭 Issue。
    写失败不影响合并结果（PR 已经合了），只记日志、下一轮同步/再点一次即可。

    :param issue_number: Issue 编号
    :param pr_number: PR 编号（只用于日志）
    :param why: 触发原因（写进日志与操作日志）
    :return: 是否成功把 Issue 标成 done
    """
    try:
        # 先换标签（保留 bug 之类的业务标签，只替换 todo/doing/done）
        github_client.set_issue_status(issue_number, "done")
        github_client.update_issue(issue_number, state="closed")
    except GitHubError as exc:
        logger.error("PR #%s 已合并，但把 Issue #%s 标成 done 失败：%s",
                     pr_number, issue_number, exc.message)
        database.log_operation(_me(), "finalize_pr", issue_number=issue_number,
                               result="error", detail=f"{why}；{exc.message}")
        return False

    database.update_task_status(issue_number, "done")
    database.log_operation(_me(), "finalize_pr", issue_number=issue_number,
                           old_value="doing", new_value="done", detail=why)
    logger.info("Issue #%s 已标成 done 并关闭（%s）", issue_number, why)
    return True


@app.route("/api/tasks/<int:issue_number>/pr/merge", methods=["POST"])
@auth.api_login_required
def merge_task_pr(issue_number: int):
    """
    **组长合并 PR**（默认 squash）。

    合并前的闸门（缺一不可，``force=1`` 可以让组长强行越过 Agent 这一条）：

    1. 你是组长；
    2. 这个 PR 已经「放行」（组长点过允许）；
    3. Agent 已经审核通过（现读 Issue 评论里最新的报告，不依赖本地缓存）。

    合并后看板会立刻替你把 Issue 标成 ``done`` 并关闭 —— 因为组员自己在 GitHub 上提的 PR
    未必带 ``Closes #编号``，而 Agent 在 PR 模式下是只审核不定案的。
    这样「Agent 过审 + 组长同意合并」两个条件都满足时，卡片就会进 Done。
    """
    message = auth.check_lead(_me())
    if message:
        return _deny(auth.lead_only_message("合并 PR"), "merge_pr", issue_number)

    task = database.get_task_by_issue(issue_number)
    if not task:
        return fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)
    pr_number = task.get("pr_number")
    if not pr_number:
        return fail(f"#{issue_number} 还没有 PR", 400)

    data = _parse_json_body()
    method = str(data.get("method") or PR_MERGE_METHOD).lower()
    force = bool(data.get("force"))

    if not task.get("pr_allowed"):
        return _deny("这个 PR 还没放行：请先点「允许」再合并", "merge_pr", issue_number)

    # ---- 闸门 3：Agent 必须过审（现读最新报告）----
    review = _latest_agent_verdict(issue_number)
    if review is None:
        if not force:
            return _deny(
                "这个任务的 Issue 下还没有 Agent 审核报告：等 Agent 审完（提交信息里带上 Issue 编号，"
                "例如 `#%d`）再合并；确实要先合就勾上「强制合并」" % issue_number,
                "merge_pr", issue_number)
        logger.warning("组长强制合并 PR #%s：Issue #%s 还没有 Agent 审核报告",
                       pr_number, issue_number)
    elif not review.get("passed") and not force:
        score = review.get("score")
        comment = (review.get("comment") or "见 Issue 评论").strip().replace("\n", " ")
        if len(comment) > 90:
            comment = comment[:90] + "…"
        return _deny(
            f"Agent 审核没有通过（{('评分 %s，' % score) if score is not None else ''}"
            f"最新评语：{comment}）：先按建议改代码再合并；"
            f"确实要合就勾上「强制合并」",
            "merge_pr", issue_number)

    try:
        result = github_client.merge_pull_request(int(pr_number), method)
    except GitHubError as exc:
        logger.error("合并 PR #%s 失败：%s", pr_number, exc.message)
        database.log_operation(_me(), "merge_pr", issue_number=issue_number,
                               result="error", detail=exc.message)
        return fail(exc.message, exc.status_code or 502)

    database.update_task_pr(issue_number, pr_state="merged")

    # ---- 收尾：标 done + 关闭 Issue（失败也不影响「已合并」这个事实）----
    finalized = _finalize_merged_issue(issue_number, pr_number, why=f"PR #{pr_number} 已合并（{method}）")

    database.log_operation(_me(), "merge_pr", issue_number=issue_number,
                           new_value=f"PR #{pr_number}（{method}）")
    realtime.notify_tasks_changed("merge_pr", issue=issue_number, by=_me(),
                                  detail=f"已合并 PR #{pr_number}")
    tail = ("Issue 已标成 done 并关闭，卡片就在 Done 列了"
            if finalized else
            "Issue 的 done 标签没写成功（可能 Token 权限不足），卡片可以点 PR 面板里的"
            "「标记完成并关闭 Issue」重试")
    return ok(pr_number=pr_number, merged=True, finalized=finalized,
              sha=result.get("sha", ""),
              message=f"PR #{pr_number} 已合并（{method}）。{tail}")


@app.route("/api/tasks/<int:issue_number>/pr/finalize", methods=["POST"])
@auth.api_login_required
def finalize_task_pr(issue_number: int):
    """
    **补一次收尾**：把已合并的 PR 对应的 Issue 标成 done 并关闭。

    用于这些情况：PR 早就合了（例如在 GitHub 上直接合的，或者在这条规则上线之前合的），
    Issue 却还开着 / 还挂着 doing，卡片赖在 Doing 列。只有组长能调用。
    """
    message = auth.check_lead(_me())
    if message:
        return _deny(auth.lead_only_message("标记完成"), "finalize_pr", issue_number)

    task = database.get_task_by_issue(issue_number)
    if not task:
        return fail(f"本地不存在任务 #{issue_number}，请先执行同步", 404)
    if not task.get("pr_number"):
        return fail(f"#{issue_number} 还没有 PR，不需要收尾", 400)

    # 没记成 merged 的话再问一次 GitHub，避免本地状态落后导致误操作
    if task.get("pr_state") != "merged":
        try:
            pr = github_client.get_pull_request(int(task["pr_number"]))
        except GitHubError as exc:
            return fail(f"读取 PR 状态失败：{exc.message}", exc.status_code or 502)
        if pr.get("state") != "merged":
            return fail(f"PR #{task['pr_number']} 还没合并（当前 {pr.get('state')}），先合并再说", 400)
        database.update_task_pr(issue_number, pr_state="merged")

    finalized = _finalize_merged_issue(issue_number, task.get("pr_number"),
                                      why="组长手动补收尾（已合并的 PR）")
    if not finalized:
        return fail("写入 GitHub 失败：请检查 Token 是否有 Issues 读写权限", 502)

    realtime.notify_tasks_changed("finalize_pr", issue=issue_number, by=_me(),
                                  detail="已标记完成并关闭 Issue")
    return ok(pr_number=task.get("pr_number"), status="done",
              message=f"#{issue_number} 已标成 done 并关闭 Issue，卡片进 Done 了")


@app.route("/api/tasks/restore-all", methods=["POST"])
@auth.api_login_required
def restore_all_tasks():
    """把全部「已从看板移除」的任务恢复显示。"""
    hidden_tasks = database.get_all_tasks(include_hidden=True)
    restored = 0
    for task in hidden_tasks:
        if task.get("hidden"):
            database.hide_task(task["issue_number"], False)
            restored += 1

    database.log_operation(_me(), "restore_task", new_value=f"全部（{restored} 条）")
    if restored:
        realtime.notify_tasks_changed("restore_task", by=_me(), detail=f"恢复了 {restored} 条")
    return ok(message=f"已恢复 {restored} 条任务的显示", restored=restored, hidden_count=0)


# ============================================================
# 同步
# ============================================================
def _collect_pr_index() -> dict[str, dict[str, Any]]:
    """
    拉一份「分支 → PR」的索引（1 次分页请求）。

    用途：别人直接在 GitHub 上提的 PR 也能被看板认领到卡片上，
    不要求必须从看板点「提 PR」。

    :return: {head 分支名: 归一化 PR}
    """
    index: dict[str, dict[str, Any]] = {}
    try:
        for pr in github_client.list_pull_requests(state="all", max_pages=1):
            head = str(pr.get("head") or "")
            if head:
                # 同一分支有多个 PR 时，open 的优先（覆盖已合并/关闭的旧记录）
                if head not in index or pr.get("state") in ("open", "draft"):
                    index[head] = pr
    except GitHubError as exc:
        logger.warning("拉取 PR 列表失败（本次同步跳过 PR 认领）：%s", exc.message)
    return index


def _pr_snapshot(task_number: int, branch: str, local: Optional[dict[str, Any]],
                 pr_index: dict[str, dict[str, Any]],
                 budget: dict[str, int]) -> Optional[dict[str, Any]]:
    """
    算出这张卡片当前的 PR 状态。

    * 卡片上已经记了 PR 编号 → 单独查一次它（拿最新的 merged / closed 状态）
    * 否则用分支名去索引里认领（别人在 GitHub 上提的 PR）

    :param budget: 共享的预算计数器 {"used": n}
    :return: 归一化 PR；没有或超预算时返回 None
    """
    pr_number = (local or {}).get("pr_number")
    branch = str(branch or (local or {}).get("branch") or "")
    if pr_number:
        if budget["used"] >= PR_SYNC_BUDGET:
            budget["hit"] = 1
            return None
        budget["used"] += 1
        try:
            current = github_client.get_pull_request(int(pr_number))
        except GitHubError as exc:
            logger.warning("刷新 PR #%s 状态失败：%s", pr_number, exc.message)
            current = None

        # 卡片上记的那个 PR 已经关了（但没合并）→ 同一分支上可能又开了一个新的，
        # 这种情况要学会自己改认新的那个，否则卡片会一直指着作废的 PR。
        if current and current.get("state") == "closed" and branch in pr_index:
            newer = pr_index[branch]
            if newer.get("number") != current.get("number"):
                logger.info("卡片记的 PR #%s 已关闭，改认同一分支上更新的 PR #%s",
                            pr_number, newer.get("number"))
                return newer
        return current

    if branch and branch in pr_index:
        return pr_index[branch]
    return None


def _run_sync(with_meta: bool, prune: bool, with_reviews: bool = True) -> dict[str, Any]:
    """
    执行一次完整同步。

    :param with_meta: 是否同时抓取关联分支与提交（会多消耗若干次 API 额度）
    :param prune: 是否清理本地已不存在的任务
    :param with_reviews: 是否读回 Issue 评论里的 Agent 审核报告
    :return: 同步统计信息
    """
    issues = github_client.fetch_all_issues(state=SYNC_ISSUE_STATE, max_pages=SYNC_MAX_PAGES)

    collector: RepoMetaCollector | None = None
    pr_index: dict[str, dict[str, Any]] = {}
    if with_meta and issues:
        collector = RepoMetaCollector(
            branch_commit_budget=SYNC_BRANCH_COMMIT_BUDGET,
            commits_per_task=COMMITS_PER_TASK,
        )
        collector.prepare()
        pr_index = _collect_pr_index()

    created = updated = unchanged = 0
    assigned_count = 0
    reviewed_count = 0
    pr_count = 0
    review_requests = 0
    review_budget_hit = False
    pr_budget = {"used": 0, "hit": 0}
    merged_not_done: list[int] = []
    changes: list[dict[str, Any]] = []
    valid_numbers: list[int] = []

    # 同步时会比对的「用户可见字段」：权威来源是 GitHub
    visible_fields = (
        ("status", "状态"),
        ("assignee", "负责人"),
        ("title", "标题"),
        ("state", "Issue 状态"),
    )

    for issue in issues:
        number = int(issue["number"])
        valid_numbers.append(number)

        status = github_client.status_from_labels(issue)

        # 从 body 解析负责人（None 表示 body 中没有该标记，保留本地已有值）
        assignee, description = github_client.split_issue_body(issue.get("body") or "")
        if assignee:
            assigned_count += 1

        local = database.get_task_by_issue(number)

        # ---- Agent 审核结果（写在 Issue 评论里）----
        review: dict[str, Any] | None = None
        comment_count = int(issue.get("comments") or 0)
        if with_reviews and comment_count > 0:
            if review_requests >= SYNC_REVIEW_BUDGET:
                if not review_budget_hit:
                    logger.warning("本次同步已达评论抓取预算（%d 个 Issue），"
                                   "其余任务本轮不解析审核结果", SYNC_REVIEW_BUDGET)
                    review_budget_hit = True
            else:
                review_requests += 1
                try:
                    comments = github_client.get_issue_comments(number)
                    review = github_client.parse_review_from_comments(comments)
                except GitHubError as exc:  # 单个任务失败不应中断整体同步
                    logger.warning("解析任务 #%s 的审核评论失败：%s", number, exc.message)

            if review:
                reviewed_count += 1

        meta: dict[str, Any] = {}
        if collector is not None:
            try:
                meta = collector.collect(number)
            except GitHubError as exc:  # 单个任务失败不应中断整体同步
                logger.warning("收集任务 #%s 的分支/提交失败：%s", number, exc.message)

        # ---- PR：卡片上已有 PR 就刷新它的状态；没有就用分支名认领一个 ----
        pr = _pr_snapshot(number, meta.get("branch") or "", local, pr_index, pr_budget)
        remote_pr: Optional[dict[str, Any]] = None
        if pr and pr.get("number"):
            remote_pr = {
                "pr_number": pr["number"],
                "pr_state": pr.get("state") or "",
                "pr_url": pr.get("url") or "",
                "pr_head": pr.get("head") or "",
            }
            pr_count += 1
            # 「PR 已合并、卡片却还没进 Done」= 漏了收尾（合并时写 GitHub 失败、
            # 或者是在这条规则上线之前合的）。同步只读 GitHub，不替用户改状态，
            # 但要把它列出来，提醒组长点一下「标记完成并关闭 Issue」。
            if remote_pr["pr_state"] == "merged" and status != "done":
                merged_not_done.append(number)

        # 远端派生值（None 表示“这一次没有拿到/不修改”，不参与比较）
        remote: dict[str, Any] = {
            "title": issue.get("title") or f"Issue #{number}",
            "status": status,
            "assignee": assignee,
            "state": issue.get("state", "open"),
        }

        # ---- 新增：本地没有这条就直接建 ----
        if local is None:
            database.upsert_task(
                issue_number=number,
                title=remote["title"],
                description=description,
                status=status,
                assignee=assignee,
                branch=meta.get("branch"),
                commits=meta.get("commits"),
                html_url=issue.get("html_url", ""),
                state=remote["state"],
                review_score=review.get("score") if review else None,
                review_passed=review.get("passed") if review else None,
                review_comment=review.get("text") if review else None,
                review_replace=bool(review),
            )
            if remote_pr:
                database.update_task_pr(number, **remote_pr)
            created += 1
            continue

        # ---- 已存在：先算出到底哪些字段变了 ----
        field_changes: dict[str, tuple[str, str, str]] = {}
        for field, label in visible_fields:
            new_value = remote.get(field)
            if new_value is None:
                continue        # 远端这次没给（例如 body 里没有负责人行）→ 保持本地
            old_value = local.get(field) or ""
            if str(old_value) != str(new_value):
                field_changes[field] = (str(old_value), str(new_value), label)

        # 元信息 / 审核结果 / PR 是否变化（不列进“用户可见变更”，但需要落库）
        meta_changed = False
        if collector is not None:
            if (local.get("branch") or "") != (meta.get("branch") or ""):
                meta_changed = True
            if (local.get("commits") or []) != (meta.get("commits") or []):
                meta_changed = True
        if remote_pr:
            if (local.get("pr_number") != remote_pr["pr_number"]
                    or (local.get("pr_state") or "") != remote_pr["pr_state"]
                    or (local.get("pr_url") or "") != remote_pr["pr_url"]
                    or (local.get("pr_head") or "") != remote_pr["pr_head"]):
                meta_changed = True
                # PR 状态变化对用户是有意义的，放进「被 GitHub 更新的卡片」里说清楚
                if (local.get("pr_number") and local.get("pr_state")
                        and local.get("pr_state") != remote_pr["pr_state"]):
                    field_changes["pr_state"] = (str(local.get("pr_state")),
                                                 str(remote_pr["pr_state"]), "PR 状态")
        if review is not None:
            if (local.get("review_score") != review.get("score")
                    or local.get("review_passed") != review.get("passed")
                    or (local.get("review_comment") or "") != (review.get("text") or "")):
                meta_changed = True

        # 只有真的变了才写库：避免每次同步都把 updated_at 刷成“刚刚”，
        # 让卡片上的时间重新变得有意义（这也是“同步很诡异”的来源之一）
        if not field_changes and not meta_changed:
            unchanged += 1
            continue

        database.upsert_task(
            issue_number=number,
            title=remote["title"],
            description=description,
            status=status,
            assignee=assignee,
            branch=meta.get("branch"),
            commits=meta.get("commits"),
            html_url=issue.get("html_url", ""),
            state=remote["state"],
            # 没有审核报告时传 None 并把 review_replace 设为 False，保留库里已有的审核结果；
            # 解析到报告时整组覆盖（报告里没有分数/结论就把旧值清成 NULL）
            review_score=review.get("score") if review else None,
            review_passed=review.get("passed") if review else None,
            review_comment=review.get("text") if review else None,
            review_replace=bool(review),
        )
        if remote_pr:
            database.update_task_pr(number, **remote_pr)
        updated += 1

        if field_changes:
            detail = "；".join(
                f"{label} {assignee_display(old) if field == 'assignee' else old}"
                f" → {assignee_display(new) if field == 'assignee' else new}"
                for field, (old, new, label) in field_changes.items()
            )
            changes.append({"issue": number, "detail": detail,
                            "fields": sorted(field_changes)})
            # 记进操作日志：以后想知道“我的卡片为什么变了”，一查就知道是同步带下来的
            database.log_operation(
                _me(), "sync_update", issue_number=number,
                old_value="；".join(f"{f}={v[0]}" for f, v in field_changes.items()),
                new_value="；".join(f"{f}={v[1]}" for f, v in field_changes.items()),
                detail=f"来自 GitHub（Agent / 其他人 / 网页改动）：{mask_real_names(detail)}",
            )
            logger.info("同步更新 #%s：%s", number, detail)

    removed = database.delete_missing_issues(valid_numbers) if prune else 0

    if pr_budget["hit"]:
        logger.warning("本次同步已达 PR 状态刷新预算（%d 个），其余卡片本轮不刷新 PR",
                       PR_SYNC_BUDGET)
    if merged_not_done:
        logger.warning("以下任务的 PR 已合并但状态还不是 done，需要组长补收尾：%s",
                       "、".join(f"#{n}" for n in merged_not_done))

    return {
        "total": len(valid_numbers),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "removed": removed,
        "assigned": assigned_count,
        "reviewed": reviewed_count,
        "review_requests": review_requests,
        "prs": pr_count,
        "merged_not_done": merged_not_done,
        "changes": changes,
        "with_meta": with_meta,
        "with_reviews": with_reviews,
    }


@app.route("/api/sync", methods=["POST"])
@auth.api_login_required
def sync():
    """
    从 GitHub 同步 Issue 到本地缓存（任何登录用户都可以点同步，它只读 GitHub）。

    :query with_meta: 是否抓取分支与提交，默认 1（传 0 可加速同步）
    :query with_reviews: 是否读回 Agent 审核报告（Issue 评论），默认 1
    :query prune: 是否清理本地多余任务，默认 1（传 0 保留本地历史）
    """
    body = _parse_json_body()

    def _flag(name: str, default: bool) -> bool:
        """从 query 或 JSON body 中解析布尔开关。"""
        if name in request.args:
            return str(request.args.get(name)).lower() in ("1", "true", "yes", "on")
        if name in body:
            return bool(body.get(name))
        return default

    with_meta = _flag("with_meta", True)
    with_reviews = _flag("with_reviews", True)
    prune = _flag("prune", True)

    try:
        stats = _run_sync(with_meta=with_meta, prune=prune, with_reviews=with_reviews)
    except GitHubError as exc:
        logger.error("同步失败：%s", exc.message)
        return fail(exc.message, exc.status_code or 502)
    except Exception as exc:
        logger.exception("同步过程发生未预期异常")
        return fail(f"同步失败：{exc}", 500)

    message = (
        f"同步完成：共 {stats['total']} 个任务"
        f"（新增 {stats['created']}，更新 {stats['updated']}，"
        f"未变化 {stats['unchanged']}，已分配 {stats['assigned']}，"
        f"已审核 {stats['reviewed']}，关联 PR {stats.get('prs', 0)}，清理 {stats['removed']}）"
    )

    # 把「哪些卡片被远端改了」说清楚：不再让本地改动看起来“莫名其妙被改回去”
    changes = stats.get("changes") or []
    if changes:
        listed = "；".join(f"#{item['issue']} {item['detail']}" for item in changes[:5])
        if len(changes) > 5:
            listed += f"；等共 {len(changes)} 条"
        message += f"。被 GitHub 更新的卡片（Agent / 其他人 / 网页改动）：{listed}"
        message += "。如果你确定不该这样，可按操作日志追溯（/api/logs）"

    database.log_operation(_me(), "sync", new_value=str(stats), detail=message)
    logger.info(message)
    # 同步也可能改到卡片（Agent 关 Issue、别人在网页上改），广播给所有看板
    realtime.notify_tasks_changed(
        "sync", by=_me(),
        detail=f"同步：更新 {stats['updated']}，新增 {stats['created']}，未变化 {stats['unchanged']}",
    )
    return ok(message=message, **stats)


# ============================================================
# 全局错误处理
# ============================================================
@app.errorhandler(404)
def handle_404(_error):
    """统一 404 响应（API 返回 JSON，页面仍返回提示文本）。"""
    if request.path.startswith("/api/"):
        return fail("接口不存在", 404)
    return "页面不存在", 404


@app.errorhandler(405)
def handle_405(_error):
    """统一 405 响应。"""
    return fail("请求方法不被允许", 405)


@app.errorhandler(Exception)
def handle_exception(error):
    """兜底异常处理，避免把堆栈直接暴露给前端。"""
    # 4xx/5xx 这类「本来就是 HTTP 错误」的异常交给 Werkzeug 自己渲染：
    # 否则 413/WebsocketMismatch 之类会被包成 500，把真实原因盖掉
    if isinstance(error, HTTPException):
        if request.path.startswith("/api/"):
            return fail(error.description or error.name, error.code or 500)
        return error

    logger.exception("服务端未捕获异常：%s", error)
    return fail(f"服务器内部错误：{error}", 500)


# ============================================================
# 启动
# ============================================================
def bootstrap() -> None:
    """启动前的准备工作：日志、数据库、配置检查。"""
    setup_logging()

    errors = validate_config()
    if errors:
        logger.warning("=" * 56)
        logger.warning("配置存在问题，GitHub 相关功能将不可用：")
        for item in errors:
            logger.warning("  - %s", item)
        logger.warning("请在项目根目录创建 .env（可参考 .env.example）后重启")
        logger.warning("=" * 56)

    database.init_db()
    # 绑定任务仓库：换了仓库（或缓存其实属于别的仓库）就清空旧缓存
    # Issue 编号只在单个仓库内唯一，混着用会把状态/负责人写到无关的 Issue 上
    switch = database.bind_repo(GITHUB_REPO or "")
    if switch["changed"]:
        logger.warning("=" * 56)
        logger.warning("本地任务缓存已重建：%s", switch["reason"])
        logger.warning("清掉旧任务 %d 条；用户与操作日志保留。点一次「同步」即可拉取新仓库",
                       switch["cleared"])
        logger.warning("=" * 56)
    # 首次启动时创建 6 个初始用户（已存在则跳过）
    auth.ensure_default_users()


bootstrap()

if __name__ == "__main__":
    info = build_server_info()

    logger.info("=" * 56)
    logger.info("看板服务启动中……")
    logger.info("仓库：%s", GITHUB_REPO or "(未配置)")
    logger.info("Token：%s", mask_token() or "(未配置)")
    logger.info("数据库：%s", DB_PATH)
    logger.info("负责人名单：%s", "、".join(ASSIGNEES))
    logger.info("登录用户：%d 人（初始密码见 agent 无关的 config.DEFAULT_PASSWORD）",
                len(database.list_users()))
    logger.info("权限：每人只能操作自己的任务；「完成」只能由 Agent 审核后自动写入")
    logger.info("本机访问：http://127.0.0.1:%s", PORT)
    if realtime.WEBSOCKET_AVAILABLE:
        logger.info("实时通道：WebSocket /ws 已启用（协议 v%s，心跳 %ss）",
                    realtime.PROTOCOL_VERSION, realtime.PING_INTERVAL)
    else:
        logger.warning("实时通道：未安装 simple-websocket，前端会退回轮询"
                       "（pip install simple-websocket 可启用 WebSocket）")

    if info["local_only"]:
        # 没探测到局域网地址：多半是没连热点/Wi-Fi，或没拿到 DHCP
        logger.warning("局域网访问：未检测到可用的局域网地址")
        logger.warning("  请先连上手机热点或 Wi-Fi，再重启服务")
    else:
        logger.info("组员访问：%s", info["url"])
        for extra in info["urls"][1:]:
            logger.info("  备选地址：%s", extra)
        logger.info("  （页面顶部也会显示这个地址，并可一键复制）")
        logger.info("  连不上时先检查防火墙：%s", info["firewall_hint"])

    logger.info("=" * 56)

    # threaded=True：每个 WebSocket 连接要占一个线程，必须开多线程
    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=DEBUG, threaded=True)
