"""
GitHub API 客户端
----------------
封装看板所需的全部 GitHub REST API v3 调用：

* Issue 的读取 / 创建 / 更新（标签即状态）
* 标签的自动创建（首次使用时无需手动在仓库里建 todo/doing/done）
* 关联分支的推断（按分支名中的 Issue 编号匹配）
* 关联提交的收集（提交信息里出现的 #编号）

所有网络失败都会自动重试（默认 3 次，指数退避），
Token 失效 / 权限不足 / 仓库不存在等情况抛出带中文说明的 GitHubError。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import requests

from config import (
    ASSIGNEE_BODY_PATTERN,
    ASSIGNEE_BODY_PREFIX,
    ASSIGNEE_LINE_PATTERN,
    COMMITS_PER_TASK,
    GITHUB_API_BASE,
    GITHUB_REPO,
    GITHUB_TOKEN,
    LABEL_COLORS,
    LABEL_TO_STATUS,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    REVIEW_COMMENTS_PER_ISSUE,
    REVIEW_COMMENT_MAX_CHARS,
    REVIEW_FAILED_MARKERS,
    REVIEW_REPORT_MARKERS,
    STATUS_LABELS,
    UNASSIGNED_LABEL,
    clean_assignee,
    normalize_assignee,
)

logger = logging.getLogger(__name__)


class GitHubError(Exception):
    """
    GitHub API 调用异常。

    :param message: 面向用户的错误说明（中文）
    :param status_code: HTTP 状态码，便于上层区分处理；网络异常时为 None
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化的字典。"""
        return {"error": self.message, "status_code": self.status_code}


# 复用一个 Session，保持连接、提升性能
_SESSION = requests.Session()

# 提交信息 / 分支名中引用 Issue 的写法：#123
_ISSUE_REF_RE = re.compile(r"#(\d+)\b")

# 负责人标记：解析 “**负责人**: 小明” / “**负责人**：小明”
_ASSIGNEE_BODY_RE = re.compile(ASSIGNEE_BODY_PATTERN)

# 负责人整行（用于从描述中剔除该行），大小写与前后空白都容忍
_ASSIGNEE_LINE_RE = re.compile(ASSIGNEE_LINE_PATTERN, re.IGNORECASE)


# ============================================================
# Agent 审核报告的解析规则
# ============================================================
# 与 agent/prompt.py 的 REPORT_TEMPLATE 对应，但解析尽量宽容，同时兼容两种写法：
#   1. 现在 Agent 实际产出的表格格式：
#        | 审核结果 | ✅ 通过 |
#        | 综合评分 | `88 / 100`（通过线 70） |
#        ### 📝 评语
#   2. 更简化的写法（例如手写或旧模板）：
#        **审核结果**: ✅ 通过
#        **评分**: 88
#        ### 评语
_REVIEW_RESULT_RE = re.compile(r"审核结果\s*\*{0,2}\s*[|:：]\s*\*{0,2}\s*([^\n|]+)")
_REVIEW_SCORE_RES: tuple[re.Pattern[str], ...] = (
    # 表格：| 综合评分 | `88 / 100`（通过线 70） |
    re.compile(r"综合评分\s*[|:：]\s*`?\s*(\d{1,3})\s*(?:/|／)\s*100"),
    # 简化：**评分**: 88 / | 评分 | 88 | / 评分：88分
    re.compile(r"评分\s*[|:：]\s*(?:\*\*)?\s*`?\s*(\d{1,3})"),
    # 兜底：任何 xx/100
    re.compile(r"(\d{1,3})\s*(?:/|／)\s*100"),
)
# 评语区块：从 “### 评语” 到下一个同级/更高级标题
_REVIEW_COMMENT_RE = re.compile(
    r"^#{2,4}\s*(?:📝\s*)?评语\s*\n(.*?)(?=\n#{1,4}\s|\Z)", re.MULTILINE | re.DOTALL
)
# 改进建议区块
_REVIEW_SUGGESTION_RE = re.compile(
    r"^#{2,4}\s*(?:💡\s*)?改进建议\s*\n(.*?)(?=\n#{1,4}\s|\Z)", re.MULTILINE | re.DOTALL
)
# 报告里的提交号：| 关联提交 | `abc1234` ... |  或隐藏标记 <!-- agent-review:abc1234 -->
_REVIEW_COMMIT_RE = re.compile(r"关联提交\s*\|\s*`?([0-9a-fA-F]{7,40})`?")
_REVIEW_MARKER_RE = re.compile(r"<!--\s*agent-review:([0-9a-fA-F]{7,40})")


# ============================================================
# 底层请求
# ============================================================
def _headers() -> dict[str, str]:
    """构造 GitHub API 请求头（含鉴权信息）。"""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "kanban-client/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _check_configured() -> None:
    """在发请求前检查必要的配置，避免发出必然失败的请求。"""
    if not GITHUB_TOKEN:
        raise GitHubError("GitHub Token 未配置：请在 .env 中设置 GITHUB_TOKEN", None)
    if not GITHUB_REPO or "/" not in GITHUB_REPO:
        raise GitHubError("GitHub 仓库未配置或格式错误：请在 .env 中设置 GITHUB_REPO=owner/repo", None)


def _handle_status(resp: requests.Response) -> bool:
    """
    根据响应状态码决定“报错”“重试”还是“成功”。

    :return: True 表示可以重试；False 表示应当直接返回该响应
    :raises GitHubError: 不可重试的错误（401/403/404/422 等）
    """
    code = resp.status_code

    if code == 401:
        raise GitHubError(
            "GitHub Token 无效或已过期（401）：请检查 .env 中的 GITHUB_TOKEN 是否正确、是否被吊销",
            401,
        )

    if code == 403:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0":
            reset = resp.headers.get("X-RateLimit-Reset")
            hint = ""
            if reset and reset.isdigit():
                wait_min = max(0, (int(reset) - int(time.time())) // 60)
                hint = f"，约 {wait_min} 分钟后恢复"
            raise GitHubError(f"GitHub API 调用次数已用尽（403 限流）{hint}", 403)
        raise GitHubError(
            "GitHub 拒绝访问（403）：Token 可能缺少 Issues / Contents / Pull requests 的读写权限，"
            "或该仓库未被授权给此 Token",
            403,
        )

    if code == 404:
        raise GitHubError(
            f"资源不存在（404）：请确认仓库 {GITHUB_REPO!r} 是否存在、Token 是否有权访问",
            404,
        )

    if code == 422:
        detail = ""
        try:
            payload = resp.json()
            detail = payload.get("message", "")
            errors = payload.get("errors")
            if errors:
                detail += f" {errors}"
        except ValueError:
            detail = resp.text[:200]
        raise GitHubError(f"GitHub 校验失败（422）：{detail or '请求参数不合法'}", 422)

    # 5xx / 429：服务端问题，值得重试
    if code >= 500 or code == 429:
        return True

    return False


def _request(
    method: str,
    endpoint: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    retries: int = MAX_RETRIES,
    allow: tuple[int, ...] = (200, 201, 204),
) -> requests.Response:
    """
    发送带重试的 GitHub API 请求。

    :param method: HTTP 方法，如 GET / POST / PATCH
    :param endpoint: 形如 "/repos/{owner}/{repo}/issues" 的相对路径
    :param params: URL 查询参数
    :param json_body: JSON 请求体
    :param retries: 最大尝试次数（含首次）
    :param allow: 视为成功的状态码
    :return: requests.Response
    :raises GitHubError: 配置缺失、网络多次失败、鉴权/权限/校验错误
    """
    _check_configured()

    url = endpoint if endpoint.startswith("http") else f"{GITHUB_API_BASE}{endpoint}"
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            resp = _SESSION.request(
                method,
                url,
                headers=_headers(),
                params=params,
                json=json_body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            # 网络层面的异常（超时、DNS、连接重置等）→ 重试
            last_error = exc
            logger.warning("GitHub 请求异常（第 %d/%d 次）：%s %s -> %s",
                           attempt, retries, method, url, exc)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            raise GitHubError(
                f"网络请求失败（已重试 {retries} 次）：{exc}", None
            ) from exc

        # 限流余量提示
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) < 10:
            logger.warning("GitHub API 剩余额度仅 %s 次，请稍后重试或减少同步频率", remaining)

        should_retry = _handle_status(resp)  # 不可恢复的错误会在这里抛出

        if should_retry:
            logger.warning("GitHub 服务端错误 %s（第 %d/%d 次）：%s %s",
                           resp.status_code, attempt, retries, method, url)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * attempt)
                continue
            raise GitHubError(
                f"GitHub 服务端持续返回 {resp.status_code}，请稍后重试", resp.status_code
            )

        if resp.status_code not in allow:
            raise GitHubError(
                f"GitHub 返回意外状态码 {resp.status_code}：{resp.text[:200]}",
                resp.status_code,
            )

        return resp

    # 理论上不会走到这里
    raise GitHubError(f"GitHub 请求失败：{last_error}", None)


def _paginate(
    endpoint: str,
    *,
    params: Optional[dict[str, Any]] = None,
    max_pages: int = 5,
    per_page: int = 100,
) -> list[dict[str, Any]]:
    """
    自动翻页拉取列表型接口，直到没有下一页或达到 max_pages。

    :param endpoint: API 相对路径
    :param params: 额外查询参数
    :param max_pages: 最多翻页数（保护接口额度）
    :param per_page: 每页数量，GitHub 上限为 100
    :return: 合并后的对象列表
    """
    results: list[dict[str, Any]] = []
    base_params = dict(params or {})
    base_params["per_page"] = per_page

    for page in range(1, max_pages + 1):
        query = dict(base_params, page=page)
        resp = _request("GET", endpoint, params=query)
        try:
            chunk = resp.json()
        except ValueError:
            logger.error("GitHub 返回内容不是合法 JSON：%s", resp.text[:200])
            break

        if not isinstance(chunk, list) or not chunk:
            break

        results.extend(chunk)

        if len(chunk) < per_page:  # 最后一页
            break

    return results


# ============================================================
# 账号 / 仓库
# ============================================================
def validate_token() -> dict[str, Any]:
    """
    校验 Token 是否有效，并顺带校验仓库是否可访问。

    :return: {'login': 用户名, 'repo': 仓库全名, 'private': 是否私有}
    :raises GitHubError: Token 无效 / 仓库不可访问
    """
    user_resp = _request("GET", "/user")
    user = user_resp.json()

    repo_resp = _request("GET", f"/repos/{GITHUB_REPO}")
    repo = repo_resp.json()

    return {
        "login": user.get("login", ""),
        "repo": repo.get("full_name", GITHUB_REPO),
        "private": bool(repo.get("private")),
        "permissions": repo.get("permissions", {}),
    }


def ensure_status_labels() -> list[str]:
    """
    确保仓库中存在 todo / doing / done 三个标签，缺失则自动创建。

    已存在时 GitHub 返回 422，这里直接忽略。

    :return: 本次新建成功的标签名列表
    """
    created: list[str] = []
    descriptions = {
        "todo": "待办：尚未开始的任务",
        "doing": "进行中：正在处理的任务",
        "done": "已完成：验收通过的任务",
    }

    for status in STATUS_LABELS:
        label = STATUS_LABELS[status]
        url = f"/repos/{GITHUB_REPO}/labels"
        try:
            _request(
                "POST",
                url,
                json_body={
                    "name": label,
                    "color": LABEL_COLORS.get(status, "ededed"),
                    "description": descriptions.get(status, ""),
                },
                allow=(200, 201),
            )
            created.append(label)
            logger.info("已创建 GitHub 标签：%s", label)
        except GitHubError as exc:
            if exc.status_code == 422:
                # 标签已存在，属于正常情况
                continue
            raise

    return created


# ============================================================
# Issue 相关
# ============================================================
def fetch_all_issues(state: str = "all", max_pages: int = 5) -> list[dict[str, Any]]:
    """
    拉取仓库中的 Issue 列表（自动翻页，自动剔除 Pull Request）。

    :param state: open / closed / all
    :param max_pages: 最多翻页数
    :return: Issue 字典列表
    """
    endpoint = f"/repos/{GITHUB_REPO}/issues"
    raw = _paginate(endpoint, params={"state": state, "sort": "created", "direction": "asc"},
                    max_pages=max_pages)
    # GitHub 的 issues 接口会把 PR 也混在里面，需要剔除
    return [item for item in raw if "pull_request" not in item]


def fetch_issue(issue_number: int) -> dict[str, Any]:
    """获取单个 Issue 的完整信息。"""
    resp = _request("GET", f"/repos/{GITHUB_REPO}/issues/{issue_number}")
    return resp.json()


def create_issue(
    title: str,
    body: str = "",
    labels: Optional[list[str]] = None,
) -> dict[str, Any]:
    """
    在 GitHub 上创建 Issue。

    :param title: 标题
    :param body: 描述（支持 Markdown）
    :param labels: 标签列表，缺省时使用 ['todo']
    :return: 新建 Issue 的 JSON 数据
    """
    payload: dict[str, Any] = {"title": title, "body": body or ""}
    payload["labels"] = labels if labels else [STATUS_LABELS["todo"]]

    resp = _request(
        "POST",
        f"/repos/{GITHUB_REPO}/issues",
        json_body=payload,
        allow=(200, 201),
    )
    issue = resp.json()
    logger.info("已在 GitHub 创建 Issue #%s：%s", issue.get("number"), title)
    return issue


def update_issue(
    issue_number: int,
    *,
    labels: Optional[list[str]] = None,
    state: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> dict[str, Any]:
    """
    更新 Issue（标签 / 状态 / 标题 / 描述）。

    :param issue_number: Issue 编号
    :param labels: 新的完整标签列表（会覆盖原有标签）
    :param state: open 或 closed
    :param title: 新标题
    :param body: 新描述
    :return: 更新后的 Issue 数据
    """
    payload: dict[str, Any] = {}
    if labels is not None:
        payload["labels"] = labels
    if state is not None:
        payload["state"] = state
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body

    if not payload:
        return fetch_issue(issue_number)

    resp = _request("PATCH", f"/repos/{GITHUB_REPO}/issues/{issue_number}", json_body=payload)
    return resp.json()


def set_issue_status(issue_number: int, status: str) -> dict[str, Any]:
    """
    把 Issue 的状态切换为指定状态（通过标签实现）。

    会保留其他业务标签（如 bug、enhancement），只替换 todo/doing/done 三个状态标签。

    :param issue_number: Issue 编号
    :param status: todo / doing / done
    :return: 更新后的 Issue 数据
    """
    if status not in STATUS_LABELS:
        raise GitHubError(f"无效状态：{status}（只能是 {'/'.join(STATUS_LABELS)}）", 400)

    issue = fetch_issue(issue_number)
    current_labels = [item["name"] for item in issue.get("labels", [])]

    # 去掉旧的状态标签，保留其他标签
    kept = [name for name in current_labels if name not in LABEL_TO_STATUS]
    new_labels = kept + [STATUS_LABELS[status]]

    return update_issue(issue_number, labels=new_labels)


def status_from_labels(issue: dict[str, Any]) -> str:
    """
    从 Issue 的标签推断看板状态。

    **Issue 已关闭 = done，优先级高于标签**：Issue 被关闭只有两种来路 ——
    Agent 审核通过后关闭，或者 PR 合并时 ``Closes #N`` 自动关闭。
    这两种都意味着「活干完了」，所以哪怕它还挂着 `doing` / `todo` 标签，
    看板也应该显示在 Done 列（否则会出现「PR 都合并了、卡片还赖在 Doing」）。

    :param issue: GitHub Issue 字典
    :return: todo / doing / done
    """
    if issue.get("state") == "closed":
        return "done"

    labels = [item["name"] for item in issue.get("labels", [])]
    for name in labels:
        if name in LABEL_TO_STATUS:
            return LABEL_TO_STATUS[name]
    return "todo"


# ============================================================
# 负责人（人员维度）
# ============================================================
# 说明：GitHub 原生 assignee 需要对方是仓库协作者且有账号，这里按需求改用
# Issue body 中的固定标记行 “**负责人**: 姓名” 作为人员维度的唯一数据源。
def build_issue_body(description: str = "", assignee: str = "") -> str:
    """
    生成写入 GitHub 的 Issue 正文：首行为负责人标记，其余为任务描述。

    :param description: 任务描述（Markdown）
    :param assignee: 负责人；空串或未分配则写“未分配”
    :return: 完整的 Issue body 文本
    """
    name = normalize_assignee(assignee) or UNASSIGNED_LABEL
    line = f"{ASSIGNEE_BODY_PREFIX} {name}"

    body = (description or "").strip()
    # 描述里如果残留了负责人行，先剔除，避免出现两行
    body = "\n".join(
        row for row in body.splitlines()
        if not _ASSIGNEE_LINE_RE.match(row)
    ).strip()

    return f"{line}\n\n{body}" if body else line


def split_issue_body(body: str) -> tuple[Optional[str], str]:
    """
    从 Issue body 中分离负责人与描述。

    :param body: GitHub Issue 的 body 原文
    :return: (负责人, 纯净描述)
             - 负责人为 None 表示 body 里没有负责人标记（同步时应保留本地已有值）
             - 负责人为空串表示明确标记为“未分配”
    """
    text = body or ""

    # 先按需求给定的正则解析负责人
    assignee: Optional[str] = None
    match = _ASSIGNEE_BODY_RE.search(text)
    if match:
        raw = clean_assignee(match.group(1))
        assignee = normalize_assignee(raw)
        if raw and not assignee:
            logger.warning("Issue body 中的负责人 %r 不在名单内，已按未分配处理", raw)

    # 再把负责人整行从描述中剔除，保证卡片描述干净
    cleaned = "\n".join(
        row for row in text.splitlines()
        if not _ASSIGNEE_LINE_RE.match(row)
    ).strip()

    return assignee, cleaned


def parse_assignee_from_body(body: str) -> Optional[str]:
    """
    仅解析负责人（等价于 ``split_issue_body(body)[0]``，便于单独使用）。

    :param body: Issue body 原文
    :return: 负责人名字；空串表示未分配；None 表示 body 中没有该标记
    """
    return split_issue_body(body)[0]


def set_issue_assignee(
    issue_number: int,
    assignee: str,
    description: Optional[str] = None,
) -> dict[str, Any]:
    """
    修改 Issue 的负责人（重写 body 中的负责人标记行，保留原有描述）。

    :param issue_number: Issue 编号
    :param assignee: 新负责人；空串表示未分配
    :param description: 已知的任务描述；为 None 时从 GitHub 拉取最新 body 后改写
    :return: 更新后的 Issue 数据
    """
    name = normalize_assignee(assignee)

    if description is None:
        # 以 GitHub 上的最新 body 为准，避免覆盖别人手改的内容
        _, description = split_issue_body(fetch_issue(issue_number).get("body") or "")

    new_body = build_issue_body(description, name)
    result = update_issue(issue_number, body=new_body)
    logger.info("Issue #%s 的负责人已更新为 %s", issue_number, name or UNASSIGNED_LABEL)
    return result


# ============================================================
# Agent 审核结果（从 Issue 评论里读回来）
# ============================================================
def get_issue_comments(
    issue_number: int,
    per_page: int = REVIEW_COMMENTS_PER_ISSUE,
    newest: bool = True,
) -> list[dict[str, Any]]:
    """
    获取某个 Issue 的评论列表。

    默认取**最新**的一批：先请求第 1 页，若响应头里有 ``rel="last"``（说明评论超过
    一页）则直接跳去最后一页，这样最多 2 次请求就能拿到最新评论，避免把上百条
    历史评论全拉回来。

    :param issue_number: Issue 编号
    :param per_page: 每页数量（GitHub 上限 100）
    :param newest: True 表示优先返回最新的评论
    :return: 评论对象列表（按时间正序，与 GitHub 返回一致）；失败时返回空列表
    """
    size = min(max(per_page, 1), 100)

    try:
        response = _request(
            "GET",
            f"/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
            params={"per_page": size, "page": 1},
        )
    except GitHubError as exc:
        # 评论读不到不应该让整个同步失败
        logger.warning("获取 Issue #%s 的评论失败：%s", issue_number, exc.message)
        return []

    data = response.json()
    if not isinstance(data, list):
        logger.warning("Issue #%s 的评论返回结构异常，已忽略", issue_number)
        return []

    # 评论多到不止一页时，跳到最后一页拿最新的
    if newest:
        links = getattr(response, "links", None) or {}
        last = links.get("last") if isinstance(links, dict) else None
        if last and last.get("page"):
            try:
                last_response = _request(
                    "GET",
                    f"/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
                    params={"per_page": size, "page": int(last["page"])},
                )
                last_data = last_response.json()
                if isinstance(last_data, list) and last_data:
                    data = last_data
            except (GitHubError, TypeError, ValueError) as exc:
                logger.warning("获取 Issue #%s 评论最后一页失败，改用第一页：%s",
                               issue_number, exc)

    return [item for item in data if isinstance(item, dict)]


def _extract_review_suggestions(text: str) -> list[str]:
    """
    从「改进建议」区块里提取条目。

    会跳过引用块、表格、HTML 注释等非建议内容（Agent 报告末尾常跟着
    “> ✅ 已自动将 Issue 标记为 done…” 这类引用行，不能当成建议）。

    :param text: 区块正文
    :return: 建议列表（去掉序号与 Markdown 强调符）
    """
    suggestions: list[str] = []
    for line in (text or "").splitlines():
        item = line.strip()
        if not item or item.startswith((">", "#", "|", "<", "---", "```")):
            continue
        # 去掉 "1. " / "- " / "* " 之类的列表前缀
        item = re.sub(r"^(?:\d+[.、)]|[-*+])\s*", "", item)
        item = item.strip().strip("*_`").strip()
        if item:
            suggestions.append(item)
    return suggestions


def parse_review_report(body: str) -> Optional[dict[str, Any]]:
    """
    解析单条评论，判断它是不是 Agent 审核报告并提取结论。

    :param body: 评论正文（Markdown）
    :return: 字典 {'passed', 'score', 'comment', 'suggestions', 'text', 'commit', 'is_error'}；
             不是审核报告或完全解析不出结论时返回 None（调用方据此降级为“不显示徽章”）
    """
    text = body or ""
    if not any(marker in text for marker in REVIEW_REPORT_MARKERS):
        return None

    # ---- 审核结果 ----
    passed: Optional[bool] = None
    result_match = _REVIEW_RESULT_RE.search(text)
    result_text = result_match.group(1).strip() if result_match else ""
    if result_text:
        if "不通过" in result_text or "未通过" in result_text:
            passed = False
        elif "通过" in result_text:
            passed = True

    # ---- 评分 ----
    score: Optional[int] = None
    for pattern in _REVIEW_SCORE_RES:
        match = pattern.search(text)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 100:
            score = value
            break

    # ---- 评语与改进建议 ----
    comment = ""
    comment_match = _REVIEW_COMMENT_RE.search(text)
    if comment_match:
        comment = comment_match.group(1).strip()

    suggestions: list[str] = []
    suggestion_match = _REVIEW_SUGGESTION_RE.search(text)
    if suggestion_match:
        suggestions = _extract_review_suggestions(suggestion_match.group(1))

    # 评语拿不到时，退化为取报告里的普通段落（跳过表格/标题/页脚等噪音）
    if not comment:
        plain: list[str] = []
        for row in text.splitlines():
            stripped = row.strip()
            if not stripped or stripped.startswith(("|", "#", "---", "<", ">", "<!--")):
                continue
            plain.append(stripped)
        comment = "\n".join(plain[:6]).strip()

    # ---- 判定这次审核是不是“没跑成”（LLM/网络故障，Agent 留的降级报告）----
    is_error = any(marker in text for marker in REVIEW_FAILED_MARKERS)

    # 既没有结论也没有评分 → 视为解析失败，交给调用方降级
    if passed is None and score is None and not is_error:
        logger.warning("评论里像审核报告但解析不出结论，已跳过")
        return None

    if is_error:
        # 审核未完成：不给通过/不通过结论，也不显示分数
        passed = None
        score = None

    commit = ""
    commit_match = _REVIEW_COMMIT_RE.search(text) or _REVIEW_MARKER_RE.search(text)
    if commit_match:
        commit = commit_match.group(1)

    # ---- 合并成一段可直接展示的评语文本 ----
    parts: list[str] = []
    if comment:
        parts.append(comment)
    if suggestions:
        listed = "\n".join(f"{index}. {item}" for index, item in enumerate(suggestions, 1))
        parts.append(f"💡 改进建议\n{listed}")
    merged = "\n\n".join(parts).strip()
    if len(merged) > REVIEW_COMMENT_MAX_CHARS:
        merged = merged[:REVIEW_COMMENT_MAX_CHARS].rstrip() + "…"

    return {
        "passed": passed,
        "score": score,
        "comment": comment,
        "suggestions": suggestions,
        "text": merged,
        "commit": commit,
        "is_error": is_error,
    }


def parse_review_from_comments(comments: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """
    从 Issue 的评论列表里提取**最新一条** Agent 审核报告。

    同一张 Issue 可能被多次审核（每次 push 都会留一份报告），因此从最新的评论
    往前找，遇到第一份能解析出结论的报告就返回；解析不了的评论直接跳过。

    :param comments: get_issue_comments() 的返回值（时间正序）
    :return: 审核结果字典（额外带 created_at / comment_id）；没有任何报告时返回 None
    """
    if not isinstance(comments, list) or not comments:
        return None

    for comment in reversed(comments):  # 从最新往前找
        if not isinstance(comment, dict):
            continue
        review = parse_review_report(comment.get("body") or "")
        if review is None:
            continue

        review["created_at"] = comment.get("created_at") or ""
        review["comment_id"] = comment.get("id")
        review["comment_url"] = comment.get("html_url") or ""
        logger.info("解析到审核报告：%s（评分 %s，提交 %s）",
                    "未完成" if review["is_error"] else
                    ("通过" if review["passed"] else "不通过"),
                    review["score"], review["commit"] or "-")
        return review

    return None


# ============================================================
# 分支与提交
# ============================================================
def normalize_commit(raw: dict[str, Any]) -> dict[str, Any]:
    """
    把 GitHub 的 commit 对象裁剪成前端需要的最小结构。

    :param raw: GitHub commit 对象
    :return: {'sha', 'message', 'date', 'author', 'url'}
    """
    commit = raw.get("commit", {}) or {}
    author = commit.get("author") or commit.get("committer") or {}
    message = (commit.get("message") or "").strip()
    return {
        "sha": (raw.get("sha") or "")[:7],
        "message": message.splitlines()[0] if message else "",  # 只取首行
        "date": author.get("date", ""),
        "author": author.get("name", ""),
        "url": raw.get("html_url", ""),
    }


def fetch_branches(max_pages: int = 3) -> list[str]:
    """
    拉取仓库的分支名列表。

    :param max_pages: 最多翻页数（每页 100 个分支）
    :return: 分支名列表
    """
    data = _paginate(f"/repos/{GITHUB_REPO}/branches", max_pages=max_pages)
    return [item.get("name", "") for item in data if item.get("name")]


def find_branch_for_issue(issue_number: int, branches: list[str]) -> Optional[str]:
    """
    按分支名推断 Issue 关联的分支。

    支持常见命名：``#12``、``12-fix``、``issue-12``、``issue/12``、``feature/12-xxx``、
    ``feature/xxx-12``、``fix_12`` 等，取最匹配（编号作为独立片段出现）的第一个分支。

    :param issue_number: Issue 编号
    :param branches: 分支名列表
    :return: 匹配到的分支名；没有匹配时返回 None
    """
    if not branches:
        return None

    # 编号必须作为独立片段出现（前后是字符串边界或 / _ - # 等分隔符）
    pattern = re.compile(rf"(?:^|[/_\-#])0*{issue_number}(?:$|[/_\-#])")

    for name in sorted(branches):
        if pattern.search(name):
            return name
    return None


# ============================================================
# 分支 / Pull Request
# ============================================================
def _repo_owner() -> str:
    """当前仓库的 owner（PR 的 head 过滤需要 owner:branch 形式）。"""
    return GITHUB_REPO.split("/", 1)[0] if "/" in GITHUB_REPO else ""


def post_issue_comment(issue_number: int, body: str) -> dict[str, Any]:
    """
    在 Issue（或 PR，两者编号通用）下面发一条评论。

    :param issue_number: Issue / PR 编号
    :param body: 评论正文（Markdown）
    :return: 评论对象
    """
    resp = _request("POST", f"/repos/{GITHUB_REPO}/issues/{issue_number}/comments",
                    json_body={"body": body})
    return resp.json()


def get_branch_sha(branch: str) -> str:
    """取某个分支当前指向的 commit SHA。"""
    resp = _request("GET", f"/repos/{GITHUB_REPO}/git/ref/heads/{branch}")
    return resp.json()["object"]["sha"]


def create_branch(branch: str, from_branch: str = "main") -> dict[str, Any]:
    """
    从 from_branch 建一个新分支。

    **幂等**：分支已存在时不报错，直接返回现有分支（看板上重复点「建分支」不会炸）。
    GitHub 对「引用已存在」返回 422，而 422 在 _request 里会被统一抛成 GitHubError，
    所以这里显式接住它、再确认一次分支确实存在。

    :param branch: 新分支名
    :param from_branch: 基于哪个分支，默认 main
    :return: {"name", "created", "sha"}
    """
    name = (branch or "").strip()
    if not name:
        raise GitHubError("分支名不能为空", 400)
    for bad in (" ", "..", "~", "^", ":", "?", "*", "\\", "//"):
        if bad in name:
            raise GitHubError(f"分支名不能包含 {bad!r}：{name}", 400)
    if name.startswith(("/", ".", "-")) or name.endswith(("/", ".")):
        raise GitHubError(f"分支名不合法：{name}", 400)

    try:
        resp = _request("POST", f"/repos/{GITHUB_REPO}/git/refs",
                        json_body={"ref": f"refs/heads/{name}", "sha": get_branch_sha(from_branch)})
    except GitHubError as exc:
        if exc.status_code != 422:
            raise
        # 「引用已存在」→ 当成成功，把现有分支返回去
        return {"name": name, "created": False, "sha": get_branch_sha(name)}

    data = resp.json()
    logger.info("已创建分支 %s（基于 %s）", name, from_branch)
    return {"name": name, "created": True, "sha": (data.get("object") or {}).get("sha", "")}


def normalize_pull_request(raw: dict[str, Any]) -> dict[str, Any]:
    """
    把 GitHub 的 PR 对象整理成看板用的字段。

    state 取值：``open`` / ``draft`` / ``merged`` / ``closed``。
    """
    state = raw.get("state") or ""
    if raw.get("merged") or raw.get("merged_at"):
        state = "merged"
    elif raw.get("draft") and state == "open":
        state = "draft"
    head = ((raw.get("head") or {}).get("ref")) or ""
    return {
        "number": raw.get("number"),
        "state": state,
        "title": raw.get("title") or "",
        "url": raw.get("html_url") or "",
        "head": head,
        "base": ((raw.get("base") or {}).get("ref")) or "",
        "merged": bool(raw.get("merged") or raw.get("merged_at")),
        "mergeable": raw.get("mergeable"),
        "draft": bool(raw.get("draft")),
    }


def find_pull_request_for_branch(branch: str) -> Optional[dict[str, Any]]:
    """
    找出某个分支对应的 PR（先查 open，再查全部）。

    :param branch: 分支名
    :return: 归一化后的 PR；没有时返回 None
    """
    owner = _repo_owner()
    for state in ("open", "all"):
        data = _paginate(f"/repos/{GITHUB_REPO}/pulls",
                         params={"state": state, "head": f"{owner}:{branch}"}, max_pages=1)
        if data:
            return normalize_pull_request(data[0])
    return None


def create_pull_request(head: str, base: str = "main", title: str = "",
                        body: str = "", draft: bool = False) -> dict[str, Any]:
    """
    提一个 Pull Request。

    分支不存在、或分支相对 base 没有任何新提交时，GitHub 返回 422（会被统一抛成 GitHubError），
    这里接住它：如果这个分支已经有 PR，就返回已有的那个；否则把原因说清楚。

    :return: 归一化后的 PR（额外带 created 字段）
    """
    try:
        resp = _request("POST", f"/repos/{GITHUB_REPO}/pulls", json_body={
            "title": title or head, "head": head, "base": base, "body": body, "draft": draft,
        })
    except GitHubError as exc:
        if exc.status_code != 422:
            raise
        existing = find_pull_request_for_branch(head)
        if existing:
            return dict(existing, created=False)
        raise GitHubError(
            f"无法创建 PR：{exc.message.split('：', 1)[-1] or '分支不存在，或它相对 base 还没有新提交'}",
            422,
        ) from exc

    pr = normalize_pull_request(resp.json())
    logger.info("已创建 PR #%s（%s → %s）", pr["number"], head, base)
    return dict(pr, created=True)


def get_pull_request(number: int) -> dict[str, Any]:
    """取单个 PR 的最新状态。"""
    return normalize_pull_request(
        _request("GET", f"/repos/{GITHUB_REPO}/pulls/{number}").json())


def list_pull_requests(state: str = "all", max_pages: int = 1) -> list[dict[str, Any]]:
    """
    列出 PR（同步时用来把 PR 状态映射回卡片）。

    :param state: open / closed / all
    :param max_pages: 最多翻页数（保护额度）
    :return: 归一化后的 PR 列表
    """
    data = _paginate(f"/repos/{GITHUB_REPO}/pulls",
                     params={"state": state, "sort": "updated", "direction": "desc"},
                     max_pages=max_pages)
    return [normalize_pull_request(item) for item in data]


def merge_pull_request(number: int, method: str = "squash") -> dict[str, Any]:
    """
    合并 PR。

    只有组长会走到这里（权限在 server 层校验）；合并方式默认 squash。

    :raises GitHubError: 不可合并（冲突、检查未通过等）
    """
    if method not in ("merge", "squash", "rebase"):
        method = "squash"
    resp = _request("PUT", f"/repos/{GITHUB_REPO}/pulls/{number}/merge",
                    json_body={"merge_method": method}, allow=(200, 405, 409))
    if resp.status_code != 200:
        try:
            message = resp.json().get("message", "")
        except ValueError:
            message = resp.text[:160]
        raise GitHubError(f"PR #{number} 合并失败：{message or '当前不可合并（可能有冲突或检查未通过）'}",
                          resp.status_code)
    data = resp.json()
    logger.info("PR #%s 已合并（%s）", number, method)
    return {"merged": bool(data.get("merged")), "message": data.get("message", ""),
            "sha": data.get("sha", "")}


def fetch_branch_commits(branch: str, limit: int = 20) -> list[dict[str, Any]]:
    """
    获取某个分支上最近的提交。

    :param branch: 分支名
    :param limit: 最多取多少条
    :return: 归一化后的提交列表（按时间倒序，最新在前）
    """
    resp = _request(
        "GET",
        f"/repos/{GITHUB_REPO}/commits",
        params={"sha": branch, "per_page": min(max(limit, 1), 100)},
    )
    data = resp.json()
    if not isinstance(data, list):
        return []
    return [normalize_commit(item) for item in data]


def fetch_commit_index(pages: int = 2) -> dict[int, list[dict[str, Any]]]:
    """
    拉取仓库最近的提交并按“提交信息中引用的 Issue 编号”建索引。

    相比“每个 Issue 请求一次提交列表”，这种方式只需 1~2 次请求，
    适合没有独立分支、只在提交信息里写 ``#12`` 的场景。

    :param pages: 抓取最近多少页提交（每页 100 条）
    :return: {issue_number: [commit, ...]}
    """
    index: dict[int, list[dict[str, Any]]] = {}
    try:
        data = _paginate(f"/repos/{GITHUB_REPO}/commits", max_pages=pages)
    except GitHubError as exc:
        # 空仓库会返回 409，不应当让整个同步失败
        logger.warning("获取仓库提交索引失败，将跳过提交关联：%s", exc.message)
        return index

    for raw in data:
        commit = normalize_commit(raw)
        for number in _ISSUE_REF_RE.findall(commit["message"]):
            index.setdefault(int(number), []).append(commit)

    return index


class RepoMetaCollector:
    """
    批量收集 Issue 关联的分支与提交信息。

    带两个保护：
    1. 分支内提交结果按分支名缓存，同一分支只请求一次；
    2. 请求预算（默认 25 次），超出后仅使用提交索引，避免触发 GitHub 限流。
    """

    def __init__(self, branch_commit_budget: int = 25, commits_per_task: int = COMMITS_PER_TASK) -> None:
        self.budget = max(0, branch_commit_budget)
        self.commits_per_task = max(1, commits_per_task)
        self.branches: list[str] = []
        self.commit_index: dict[int, list[dict[str, Any]]] = {}
        self._branch_cache: dict[str, list[dict[str, Any]]] = {}
        self._branch_requests = 0

    def prepare(self) -> None:
        """预加载分支列表与全仓库提交索引（各 1~3 次请求）。"""
        try:
            self.branches = fetch_branches()
        except GitHubError as exc:
            logger.warning("获取分支列表失败，将只使用提交索引：%s", exc.message)
            self.branches = []

        self.commit_index = fetch_commit_index()
        logger.info("元信息预加载完成：%d 个分支，%d 个 Issue 存在提交引用",
                    len(self.branches), len(self.commit_index))

    def _commits_of_branch(self, branch: str) -> list[dict[str, Any]]:
        """获取分支提交（带缓存与预算控制）。"""
        if branch in self._branch_cache:
            return self._branch_cache[branch]

        if self._branch_requests >= self.budget:
            logger.info("分支提交查询已达预算上限（%d），剩余任务仅使用提交索引", self.budget)
            self._branch_cache[branch] = []
            return []

        self._branch_requests += 1
        try:
            commits = fetch_branch_commits(branch, limit=20)
        except GitHubError as exc:
            logger.warning("获取分支 %s 的提交失败：%s", branch, exc.message)
            commits = []

        self._branch_cache[branch] = commits
        return commits

    def collect(self, issue_number: int) -> dict[str, Any]:
        """
        收集单个 Issue 的关联信息。

        :param issue_number: Issue 编号
        :return: {'branch': 分支名或 None, 'commits': [提交, ...]}
        """
        branch = find_branch_for_issue(issue_number, self.branches)
        commits: list[dict[str, Any]] = []

        if branch:
            branch_commits = self._commits_of_branch(branch)
            # 该分支上引用了本 Issue 的提交
            commits = [c for c in branch_commits if f"#{issue_number}" in c["message"]]
            # 若没有显式引用，则退化为展示该分支的最新提交（便于了解进展）
            if not commits and branch_commits:
                commits = branch_commits[:1]

        # 与提交索引合并（去重，按 sha）
        for commit in self.commit_index.get(issue_number, []):
            if all(commit["sha"] != c["sha"] for c in commits):
                commits.append(commit)

        # 按提交时间倒序，并截取前 N 条
        commits.sort(key=lambda c: c.get("date") or "", reverse=True)
        return {"branch": branch, "commits": commits[: self.commits_per_task]}


if __name__ == "__main__":
    # 直接运行本文件可快速自检 Token / 仓库 / 标签是否可用
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        info = validate_token()
        print(f"Token 有效，登录用户：{info['login']}")
        print(f"仓库可访问：{info['repo']}（私有：{info['private']}）")
        print("新建标签：", ensure_status_labels() or "无需新建")
    except GitHubError as error:
        print("自检失败：", error.message)
