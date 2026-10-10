"""
Agent 配置模块
--------------
集中管理 Agent 需要的所有配置与常量：

* 环境变量读取（**空字符串视为未配置**，这点很关键：GitHub Actions 里未设置的
  Secret 会以空字符串注入，若直接用 os.getenv 的默认值参数是拿不到默认值的）
* 本地 .env 文件加载（项目根目录 .env → agent/.env，后者优先，便于单独存放 LLM Key）
* 阈值、超时、重试、diff 截断等常量
* Secret 脱敏工具

运行环境：既能在 GitHub Actions 里跑（只依赖环境变量），也能在本地跑（读 .env）。
"""

from __future__ import annotations

import os
from pathlib import Path

# Agent 包目录与项目根目录
AGENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AGENT_DIR.parent

# ------------------------------------------------------------
# .env 文件加载
# ------------------------------------------------------------
# 说明：这里没有用 python-dotenv，而是自己解析两份 .env，
# 目的是“系统环境变量永远优先于 .env 文件”，行为可预测、零额外依赖。
_ENV_FILES: dict[str, str] = {}


def _read_env_file(path: Path) -> dict[str, str]:
    """
    解析 .env 文件（支持 KEY=VALUE、# 注释、空行、值两侧引号）。

    :param path: .env 文件路径
    :return: 键值对字典；文件不存在时返回空字典
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    values[key] = value
    except OSError:
        return {}

    return values


def _load_env_files() -> None:
    """按“项目根目录 → agent 目录”的顺序加载 .env，后者覆盖前者。"""
    _ENV_FILES.update(_read_env_file(PROJECT_ROOT / ".env"))
    _ENV_FILES.update(_read_env_file(AGENT_DIR / ".env"))


_load_env_files()


def env(name: str, default: str = "") -> str:
    """
    读取配置项：系统环境变量 > .env 文件 > 默认值。

    空字符串一律视为“未配置”，回落到下一级。

    :param name: 配置名
    :param default: 默认值
    :return: 配置值（已去除首尾空白）
    """
    value = (os.environ.get(name) or "").strip()
    if value:
        return value
    return (_ENV_FILES.get(name) or default).strip()


def env_int(name: str, default: int) -> int:
    """读取整数配置，解析失败时使用默认值。"""
    raw = env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔配置（1/true/yes/on 视为真）。"""
    raw = env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


# ------------------------------------------------------------
# GitHub 配置
# ------------------------------------------------------------
GITHUB_API_BASE: str = env("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
GITHUB_TIMEOUT: int = env_int("GITHUB_TIMEOUT", 20)
GITHUB_MAX_RETRIES: int = env_int("GITHUB_MAX_RETRIES", 3)


def github_token() -> str:
    """
    取 GitHub Token。

    依次尝试：GITHUB_TOKEN → GH_TOKEN → GH_PAT。
    （GitHub Actions 会自动注入 GITHUB_TOKEN，本地则读项目根 .env）
    """
    return env("GITHUB_TOKEN") or env("GH_TOKEN") or env("GH_PAT")


def github_repo() -> str:
    """
    取目标仓库（owner/repo）。

    依次尝试：REPO → GITHUB_REPOSITORY（Actions 内置）→ GITHUB_REPO（看板的配置名）。
    """
    return env("REPO") or env("GITHUB_REPOSITORY") or env("GITHUB_REPO")


# ------------------------------------------------------------
# LLM 配置
# ------------------------------------------------------------
DEFAULT_LLM_API_URL: str = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_LLM_MODEL: str = "deepseek-chat"

# 审核通过线：score >= 70 才通过（需求固定值，可通过 AGENT_PASS_SCORE 覆盖）
PASS_SCORE: int = env_int("AGENT_PASS_SCORE", 70)

# LLM 请求超时与重试
LLM_TIMEOUT: int = env_int("LLM_TIMEOUT", 120)
LLM_MAX_RETRIES: int = env_int("LLM_MAX_RETRIES", 3)
LLM_TEMPERATURE: float = float(env("LLM_TEMPERATURE", "0.2") or 0.2)

# ------------------------------------------------------------
# diff 体积控制（防止超大提交把上下文撑爆 / 花冤枉钱）
# ------------------------------------------------------------
DIFF_MAX_CHARS: int = env_int("AGENT_DIFF_MAX_CHARS", 12000)
DIFF_MAX_CHARS_PER_FILE: int = env_int("AGENT_DIFF_MAX_CHARS_PER_FILE", 4000)

# 提示词里最多带上几个关联 Issue 的信息
MAX_ISSUES_IN_PROMPT: int = env_int("AGENT_MAX_ISSUES_IN_PROMPT", 3)

# ------------------------------------------------------------
# 评论去重标记
# ------------------------------------------------------------
# 报告末尾会写入 “<!-- agent-review:abc1234 -->”，重跑 Action 时据此跳过重复评论
REVIEW_MARKER_PREFIX: str = "<!-- agent-review"


def review_marker(commit_sha: str) -> str:
    """
    生成某次提交的审核标记。

    :param commit_sha: 完整或短 commit sha
    :return: HTML 注释形式的标记（Markdown 渲染后不可见）
    """
    return f"{REVIEW_MARKER_PREFIX}:{(commit_sha or '')[:7]} -->"


def mask_secret(secret: str, keep: int = 4) -> str:
    """
    对密钥做脱敏，用于日志输出。

    :param secret: 原始密钥
    :param keep: 首尾保留的字符数
    :return: 形如 "sk-a***xyz" 的字符串；未配置时返回 "(未配置)"
    """
    secret = (secret or "").strip()
    if not secret:
        return "(未配置)"
    if len(secret) <= keep * 2:
        return secret[:keep] + "***"
    return f"{secret[:keep]}***{secret[-keep:]}"
