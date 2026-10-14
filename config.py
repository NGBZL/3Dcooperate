"""
配置模块
--------
统一从环境变量（或项目根目录下的 .env 文件）读取 GitHub Token、仓库信息等配置。

配置项优先级：系统环境变量 > .env 文件 > 代码内默认值
注意：本文件只负责“读配置”，不负责“校验配置”，校验逻辑见 validate_config()。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from typing import Any, Optional

try:
    # 标准做法：使用 python-dotenv 读取 .env
    from dotenv import load_dotenv

    _HAS_DOTENV = True
except ImportError:  # pragma: no cover - 仅在未安装依赖时触发
    _HAS_DOTENV = False

# ============================================================
# 基础路径
# ============================================================
# 项目根目录（即本文件所在目录），保证在任何工作目录下启动都能找对文件
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _parse_env_file(path: str) -> dict[str, str]:
    """
    极简 .env 解析器（python-dotenv 未安装时的降级实现）。

    支持 ``KEY=VALUE``、``# 注释``、空行、以及值两侧的单/双引号。

    :param path: .env 文件路径
    :return: 键值对字典；文件不存在时返回空字典
    """
    values: dict[str, str] = {}
    if not os.path.isfile(path):
        return values

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                values[key] = value
    return values


def _load_env(path: str) -> None:
    """
    加载 .env 文件到环境变量（不覆盖已存在的系统环境变量）。

    :param path: .env 文件路径
    """
    if _HAS_DOTENV:
        load_dotenv(path)
        return

    for key, value in _parse_env_file(path).items():
        os.environ.setdefault(key, value)


# 加载 .env 文件（显式指定路径，避免受当前工作目录影响）
_load_env(os.path.join(BASE_DIR, ".env"))


def _env(name: str, default: str = "") -> str:
    """读取环境变量并去除首尾空白（避免复制粘贴 Token 时带空格/换行）。"""
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    """读取整数型环境变量，解析失败时回退到默认值。"""
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量，支持 1/true/yes/on 等写法。"""
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


# ============================================================
# GitHub 配置
# ============================================================
# Personal Access Token（建议使用 Fine-grained token）
GITHUB_TOKEN: str = _env("GITHUB_TOKEN")

# 目标仓库，格式："owner/repo"，例如 "chengyu/3d-reconstruction"
GITHUB_REPO: str = _env("GITHUB_REPO")

# GitHub REST API 基地址（GitHub Enterprise 可改成自建地址）
GITHUB_API_BASE: str = _env("GITHUB_API_BASE", "https://api.github.com").rstrip("/")

# 单次请求超时（秒）
REQUEST_TIMEOUT: int = _env_int("REQUEST_TIMEOUT", 10)

# 网络失败时的重试次数与退避基数（秒）：第 n 次失败后等待 RETRY_BACKOFF * n 秒
MAX_RETRIES: int = _env_int("MAX_RETRIES", 3)
RETRY_BACKOFF: int = _env_int("RETRY_BACKOFF", 2)

# ============================================================
# 数据库配置
# ============================================================
# SQLite 文件路径，默认放在项目根目录下的 kanban.db
DB_PATH: str = _env("DB_PATH") or os.path.join(BASE_DIR, "kanban.db")

# ============================================================
# 服务配置
# ============================================================
HOST: str = _env("HOST", "0.0.0.0")
PORT: int = _env_int("PORT", 5000)
DEBUG: bool = _env_bool("DEBUG", True)

# 日志文件路径（同时输出到控制台）
LOG_PATH: str = _env("LOG_PATH") or os.path.join(BASE_DIR, "kanban.log")


# ============================================================
# 登录 / 会话配置
# ============================================================
def _load_secret_key() -> str:
    """
    取得 Flask 会话用的 SECRET_KEY。

    优先级：环境变量 SECRET_KEY > 本地文件 secret.key > 随机生成并写入 secret.key。
    落盘是为了让服务重启后**已登录的人不掉线**；secret.key 已写进 .gitignore，
    不会被提交到 GitHub。

    :return: 会话密钥字符串
    """
    from_env = _env("SECRET_KEY")
    if from_env:
        return from_env

    path = os.path.join(BASE_DIR, "secret.key")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                saved = handle.read().strip()
            if saved:
                return saved
        except OSError:
            logging.getLogger(__name__).warning("读取 secret.key 失败，将重新生成")

    generated = secrets.token_urlsafe(48)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(generated)
        os.chmod(path, 0o600)
        logging.getLogger(__name__).info("已生成新的会话密钥：%s", path)
    except OSError:
        logging.getLogger(__name__).warning("无法写入 %s，本次会话密钥只在内存中有效", path)

    return generated


SECRET_KEY: str = _load_secret_key()

# 登录状态保持多久（小时）：太短组员要频繁重登，太长不利于安全
SESSION_LIFETIME_HOURS: int = _env_int("SESSION_LIFETIME_HOURS", 12)

# 新用户的初始密码（首次登录后建议立即修改）
DEFAULT_PASSWORD: str = _env("DEFAULT_PASSWORD", "123456")

# 连续登录失败多少次后锁定，以及锁定时长（秒）
LOGIN_MAX_ATTEMPTS: int = _env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCK_SECONDS: int = _env_int("LOGIN_LOCK_SECONDS", 60)

# 密码最短长度
MIN_PASSWORD_LENGTH: int = _env_int("MIN_PASSWORD_LENGTH", 6)

# "完成"只能由 Agent 审核后自动写入，人工把状态改成 done 会被拒绝
DONE_REQUIRES_AGENT: bool = _env_bool("DONE_REQUIRES_AGENT", True)

# "完成"是终态：进入 Done 之后**任何人（含负责人本人）都不能再从界面上把它移出去**。
# 理由：Done 是 Agent 审核通过的结果，能随手拖出来就等于绕过了审核这道闸门。
# 需要返工时把它设为 0，负责人本人就能把完成的任务退回 Doing。
DONE_IS_FINAL: bool = _env_bool("DONE_IS_FINAL", True)

# 登录只能用真实姓名（推荐）：代号只作为 GitHub/数据库内部标识，不该让人去记。
# 设为 0 则代号也能登录（真名映射表丢失时也会自动退化为此行为，避免整站登不进去）。
LOGIN_BY_REAL_NAME_ONLY: bool = _env_bool("LOGIN_BY_REAL_NAME_ONLY", True)

# 是否允许把任务指派给别人。
# 默认 False：组员只能「认领给自己」或「把自己名下的放回未分配」。
# 注意：**组长不受这条限制**（角色存在 users.role 里，用 python auth.py role 设置）；
# 把这里设为 True 等于「所有人都是组长」。
ALLOW_ASSIGN_TO_OTHERS: bool = _env_bool("ALLOW_ASSIGN_TO_OTHERS", False)

# ============================================================
# 分支 / Pull Request
# ============================================================
# 默认主干分支名（建分支时的起点、提 PR 的 base）
DEFAULT_BRANCH: str = _env("DEFAULT_BRANCH", "main")

# 默认合并方式：merge / squash / rebase
PR_MERGE_METHOD: str = _env("PR_MERGE_METHOD", "squash").lower()

# 单次同步最多刷新多少个 PR 的状态（保护 API 额度）
PR_SYNC_BUDGET: int = _env_int("PR_SYNC_BUDGET", 25)

# 提 PR 时自动写进正文的模板（{issue} 会替换成编号）
# Closes #N 让 GitHub 在合并时自动关闭 Issue —— 这正是「合并后才进 Done」的机制
PR_BODY_TEMPLATE: str = _env("PR_BODY_TEMPLATE", "关联任务 #{issue}\n\nCloses #{issue}\n")

# ============================================================
# 卡片上的纯文本编辑器（直接在网页上提交代码）
# ============================================================
# 单个文件 / 单次提交的上限（字节）。纯源码文件远小于这个数，
# 上限主要是防误贴二进制大文件、也顺手保护 GitHub API 与 Agent 的 diff 预算。
CODE_MAX_FILE_BYTES: int = _env_int("CODE_MAX_FILE_BYTES", 300_000)
CODE_MAX_TOTAL_BYTES: int = _env_int("CODE_MAX_TOTAL_BYTES", 1_000_000)

# ============================================================
# 看板业务配置
# ============================================================
# 三个状态（同时也是 GitHub Issue 上的标签名）
STATUSES: tuple[str, ...] = ("todo", "doing", "done")

# 状态 -> 标签名 的映射（保留该字典是为了方便以后改成中文标签，如 "todo": "待办"）
STATUS_LABELS: dict[str, str] = {
    "todo": "todo",
    "doing": "doing",
    "done": "done",
}

# 标签名 -> 状态 的反向映射
LABEL_TO_STATUS: dict[str, str] = {v: k for k, v in STATUS_LABELS.items()}

# 新建标签时使用的颜色（GitHub 标签为 6 位十六进制，不带 #）
LABEL_COLORS: dict[str, str] = {
    "todo": "fbca04",   # 黄
    "doing": "1d76db",  # 蓝
    "done": "0e8a16",   # 绿
}

# 同步时拉取哪些 Issue：all=全部（关闭的 Issue 视为 done），open=仅未关闭
SYNC_ISSUE_STATE: str = _env("SYNC_ISSUE_STATE", "all")

# 同步时最多翻页数（每页 100 条，防止仓库 Issue 过多导致同步过慢）
SYNC_MAX_PAGES: int = _env_int("SYNC_MAX_PAGES", 5)

# 单次同步最多为多少个 Issue 查询“关联分支的提交”（GitHub 匿名/免费额度有限，做预算保护）
SYNC_BRANCH_COMMIT_BUDGET: int = _env_int("SYNC_BRANCH_COMMIT_BUDGET", 25)

# 每个任务在卡片上展示的提交条数上限
COMMITS_PER_TASK: int = _env_int("COMMITS_PER_TASK", 3)

# ============================================================
# Agent 代码审核结果（展示在卡片上的 AI 评语）
# ============================================================
# 单次同步最多为多少个 Issue 拉取评论（每条评论 1 次请求，做预算保护）
SYNC_REVIEW_BUDGET: int = _env_int("SYNC_REVIEW_BUDGET", 50)

# 每个 Issue 最多检查多少条评论（从最新往前找 Agent 报告）
REVIEW_COMMENTS_PER_ISSUE: int = _env_int("REVIEW_COMMENTS_PER_ISSUE", 100)

# 存库的评语长度上限（防止某条超长评论把卡片撑爆）
REVIEW_COMMENT_MAX_CHARS: int = _env_int("REVIEW_COMMENT_MAX_CHARS", 2000)

# 识别「这是 Agent 审核报告」的标题关键字（与 agent/prompt.py 的 REPORT_TEMPLATE 对应）
REVIEW_REPORT_MARKERS: tuple[str, ...] = ("Agent 代码审核报告",)

# 识别「这次审核没跑成」的降级标记：此时不显示通过/不通过，只给一条中性提示
REVIEW_FAILED_MARKERS: tuple[str, ...] = ("审核未能完成",)

# 本地缓存多久自动刷新一次前端（毫秒），仅用于前端轮询展示
FRONTEND_POLL_INTERVAL: int = _env_int("FRONTEND_POLL_INTERVAL", 30000)

# 默认任务状态
DEFAULT_STATUS: str = "todo"

# ============================================================
# 人员（负责人）配置
# ============================================================
# 重要：这里是「代号」而不是真实姓名。
#   * GitHub 上（Issue 标签/正文、评论、提交、仓库文件）只会出现代号
#   * 真实姓名只保存在本地文件 ASSIGNEE_ALIASES_PATH（已被 .gitignore 忽略）
#   * 看板界面会把代号翻译成真名显示，所以本地用起来仍然看到真名
# 固定 6 人：人员视图中的列顺序与此一致
ASSIGNEES: tuple[str, ...] = (
    "小明",
    "小红",
    "小刚",
    "小丽",
    "小强",
    "小美",
)

# 本地真名映射文件（代号 -> 真名），不提交到 Git
ASSIGNEE_ALIASES_PATH: str = _env("ASSIGNEE_ALIASES_PATH") or os.path.join(
    BASE_DIR, "assignees.local.json"
)

# 未分配在用界面上的显示名；数据库中统一存空串 ""
UNASSIGNED_LABEL: str = "未分配"

# 新建任务时的默认负责人（空串 = 未分配）
DEFAULT_ASSIGNEE: str = ""

# 写进 GitHub Issue body 的负责人行格式
ASSIGNEE_BODY_PREFIX: str = "**负责人**:"

# 从 body 中解析负责人的正则（需求指定格式：**负责人**: 小明 / **负责人**：小明）
ASSIGNEE_BODY_PATTERN: str = r"\*\*负责人\*\*[:：]\s*(\S+)"

# 用于整行剔除负责人行（同步时 description 里不应该残留这一行）
ASSIGNEE_LINE_PATTERN: str = r"^\s*\*\*负责人\*\*[:：].*$"

# 视为“未分配”的写法（大小写不敏感）
UNASSIGNED_ALIASES: frozenset[str] = frozenset(
    {"", UNASSIGNED_LABEL, "未指派", "待定", "待分配", "无", "none", "null", "unassigned", "n/a", "-", "—"}
)

# 人员视图列头的色相（紫色系，每个负责人一个色相，视觉上可区分）
ASSIGNEE_HUES: tuple[int, ...] = (265, 280, 250, 292, 272, 305)

# 真名映射缓存：{文件修改时间: 映射}，文件改动后自动重新加载，无需重启服务
_ALIASES_CACHE: dict[str, Any] = {"mtime": None, "data": {}}


def _read_assignee_aliases(path: str) -> dict[str, str]:
    """
    读取本地真名映射文件（JSON：代号 -> 真名）。

    以 ``_`` 开头的键（如 ``_说明``）会被忽略，方便在文件里写注释。
    只保留键在 ASSIGNEES 名单内的条目，其余记录告警。

    :param path: 文件路径
    :return: {代号: 真名}；文件不存在或格式错误时返回空字典
    """
    if not os.path.isfile(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logging.getLogger(__name__).warning("读取真名映射文件失败（%s）：%s", path, exc)
        return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, str] = {}
    for code, real_name in data.items():
        if str(code).startswith("_"):
            continue
        if code in ASSIGNEES and isinstance(real_name, str) and real_name.strip():
            result[code] = real_name.strip()
        else:
            logging.getLogger(__name__).warning(
                "真名映射中的条目 %r -> %r 不在代号名单内，已忽略", code, real_name
            )
    return result


def assignee_aliases() -> dict[str, str]:
    """
    获取「代号 -> 真名」映射（带缓存，文件改动后自动生效）。

    :return: 映射字典；没有配置文件时为空字典（此时界面直接显示代号）
    """
    path = ASSIGNEE_ALIASES_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _ALIASES_CACHE["data"]:
            _ALIASES_CACHE.update({"mtime": None, "data": {}})
        return {}

    if _ALIASES_CACHE["mtime"] != mtime:
        _ALIASES_CACHE.update({"mtime": mtime, "data": _read_assignee_aliases(path)})

    return _ALIASES_CACHE["data"]


def assignee_display(code: str) -> str:
    """
    代号 -> 本地显示名（真名）。

    :param code: 负责人代号
    :return: 真名；没有配置映射时原样返回代号
    """
    code = (code or "").strip()
    if not code:
        return ""
    return assignee_aliases().get(code, code)


def mask_real_names(text: str) -> str:
    """
    把文本里的真实姓名替换成代号。

    用途：写操作日志 / 落库前调用。界面提示该显示真名（方便人看），
    但日志与数据库里只保留代号，避免真名扩散到更多地方。

    :param text: 原始文本
    :return: 已把真名换成代号的文本
    """
    result = str(text or "")
    for code, real_name in assignee_aliases().items():
        if real_name and real_name in result:
            result = result.replace(real_name, code)
    return result


def clean_assignee(raw: str) -> str:
    """
    清洗从文本（GitHub body / 用户输入）中取到的负责人名字。

    去掉首尾空白，以及可能被一起匹配进来的 Markdown 装饰符（``*``、``_``、反引号）。

    :param raw: 原始文本
    :return: 清洗后的名字，可能为空串
    """
    return (raw or "").strip().strip("*_`").strip()


def assignee_code(raw: str) -> Optional[str]:
    """
    把任意输入解析成负责人**代号**（GitHub 侧使用的值）。

    依次识别：代号本身 → 本地真名（反向查映射）→ 未分配写法。
    这样无论前端传的是代号还是真名，落到 GitHub 上的都只会是代号。

    :param raw: 原始输入
    :return: 代号；空串表示未分配；None 表示既不是名单内的人也不是未分配写法
    """
    name = clean_assignee(raw)
    if name.lower() in UNASSIGNED_ALIASES:
        return ""

    if name in ASSIGNEES:
        return name

    # 允许直接传真实姓名（本地界面/脚本），自动折算成代号
    for code, real_name in assignee_aliases().items():
        if name == real_name:
            return code

    return None


def normalize_assignee(raw: str) -> str:
    """
    把任意输入规整成可直接入库的负责人值（一定是代号或空串）。

    规则：命中代号或本地真名则返回对应代号；
    写成“未分配 / 未指派 / none / -”等或无法识别时返回空串（未分配）。

    :param raw: 原始文本
    :return: 代号，或空串（表示未分配）
    """
    return assignee_code(raw) or ""


def is_valid_assignee(name: str) -> bool:
    """
    判断是否为合法的负责人取值（允许空串表示未分配）。

    :param name: 已清洗的名字（代号或真名）
    :return: 是否合法
    """
    return assignee_code(name) is not None


def mask_token(token: str = GITHUB_TOKEN) -> str:
    """
    对 Token 做脱敏处理，用于日志/控制台输出。

    :param token: 原始 Token
    :return: 形如 "ghp_ab...9f2c" 的字符串；未配置时返回空串
    """
    if not token:
        return ""
    if len(token) <= 10:
        return token[:2] + "***"
    return f"{token[:6]}...{token[-4:]}"


def validate_config() -> list[str]:
    """
    校验必填配置是否完整。

    :return: 错误信息列表；列表为空表示配置正常
    """
    errors: list[str] = []

    if not GITHUB_TOKEN:
        errors.append("GITHUB_TOKEN 未设置（请在 .env 中配置 GitHub Personal Access Token）")
    elif not (GITHUB_TOKEN.startswith(("ghp_", "github_pat_", "gho_", "ghs_")) or len(GITHUB_TOKEN) >= 40):
        errors.append("GITHUB_TOKEN 格式可疑（ghp_ / github_pat_ 开头的才是常用 Token）")

    if not GITHUB_REPO:
        errors.append("GITHUB_REPO 未设置（格式应为 'owner/repo'）")
    elif "/" not in GITHUB_REPO or GITHUB_REPO.count("/") != 1:
        errors.append(f"GITHUB_REPO 格式错误：{GITHUB_REPO!r}，应为 'owner/repo'")
    elif not all(part.strip() for part in GITHUB_REPO.split("/")):
        errors.append(f"GITHUB_REPO 的 owner 或 repo 为空：{GITHUB_REPO!r}")

    if SYNC_ISSUE_STATE not in ("all", "open"):
        errors.append(f"SYNC_ISSUE_STATE 只能是 'all' 或 'open'，当前为 {SYNC_ISSUE_STATE!r}")

    return errors


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """
    初始化全局日志：同时输出到控制台和 LOG_PATH 文件。

    :param level: 日志级别
    :return: 根日志记录器
    """
    root = logging.getLogger()
    if root.handlers:  # 避免 Flask debug 模式重复加载时重复添加 handler
        return root

    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        # 日志文件无法写入时不影响主流程，只在控制台提示
        root.warning("无法写入日志文件：%s", LOG_PATH)

    return root
