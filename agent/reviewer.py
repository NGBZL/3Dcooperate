"""
代码审核核心逻辑
----------------
职责：
1. 通过 GitHub REST API 取本次 push 的 diff 与提交信息
2. 从提交信息里提取关联的 Issue 编号
3. 调用 LLM 按 6 个维度审核，拿到严格 JSON 结论
4. 把 Markdown 审核报告写回 Issue 评论
5. 过审才把 Issue 标记为 done 并关闭；不过审只留评语

只依赖 requests，不引入 PyGithub，便于在 GitHub Actions 里零负担运行。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from . import config as cfg
from .llm_client import LLMClient, LLMError
from .prompt import (
    FAILED_FOOTER,
    PASSED_FOOTER,
    REPORT_TEMPLATE,
    SYSTEM_PROMPT,
    build_user_prompt,
)

logger = logging.getLogger(__name__)


class ReviewError(Exception):
    """
    审核流程异常（配置缺失 / GitHub API 失败等）。

    :param message: 面向用户的中文说明
    :param status_code: HTTP 状态码；网络异常时为 None
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ============================================================
# 关联 Issue 的正则
# ============================================================
# 关键词写法：close #1 / closes #1 / closed #1 / fixing #1 / fix #1 / fixes #1 /
#             fixed #1 / resolve #1 / resolves #1 / resolved #1 / resolving #1
# 用 \b 词边界，避免 "prefixes 12" 里的 "fixes" 被误当成关键词
_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|closing|fix(?:e[sd])?|fixing|resolve[sd]?|resolving)\b[:\s]*#(\d+)",
    re.IGNORECASE,
)

# 裸写法：#1（前面不能是字母/数字/#，后面不能紧跟数字，避免匹配 #123456 这类色值）
_BARE_RE = re.compile(r"(?<![\w#])#(\d{1,5})(?!\d)")


# ============================================================
# 无需送审的文件（锁文件、依赖目录、二进制/点云资产等）
# ============================================================
_IGNORED_PATH_RES = (
    re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|Cargo\.lock)$"),
    re.compile(r"(^|/)(node_modules|dist|build|\.venv|venv|__pycache__|\.idea|\.vscode)/"),
    re.compile(
        r"\.(png|jpe?g|gif|webp|bmp|ico|pdf|zip|tar|gz|whl|so|dylib|dll|exe|bin|"
        r"pcd|ply|obj|stl|glb|gltf|npy|npz|mat|pt|pth|onnx|ckpt|pb|h5|ckpt)$",
        re.IGNORECASE,
    ),
    re.compile(r"(^|/)\.DS_Store$"),
)


class CodeReviewer:
    """
    代码审核 Agent。

    :param github_token: GitHub Token；默认读配置（Actions 里的 GITHUB_TOKEN）
    :param repo: 仓库全名 owner/repo；默认读配置
    :param llm: LLM 客户端实例（便于测试时注入假实现）
    :param dry_run: 演练模式，只打印报告不写 GitHub
    """

    def __init__(
        self,
        github_token: Optional[str] = None,
        repo: Optional[str] = None,
        llm: Any = None,
        dry_run: bool = False,
    ) -> None:
        self.github_token = (
            github_token if github_token is not None else cfg.github_token()
        ).strip()
        self.repo = (repo if repo is not None else cfg.github_repo()).strip()
        self.dry_run = dry_run

        if not self.github_token:
            raise ReviewError(
                "GITHUB_TOKEN 未设置：本地运行请在项目根目录 .env 配置 GITHUB_TOKEN，"
                "GitHub Actions 中会自动注入"
            )
        if not self.repo or "/" not in self.repo:
            raise ReviewError(
                f"仓库未配置或格式错误：{self.repo!r}，应为 'owner/repo'"
                "（配置 REPO 或 GITHUB_REPO）"
            )

        # llm 允许注入，方便单元测试；否则按配置新建
        self.llm = llm if llm is not None else LLMClient()

        logger.info("CodeReviewer 就绪：repo=%s, dry_run=%s", self.repo, self.dry_run)

    # ========================================================
    # 底层 HTTP
    # ========================================================
    def _headers(self) -> dict[str, str]:
        """构造 GitHub API 请求头。"""
        return {
            "Authorization": f"Bearer {self.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "kanban-agent-review/1.0",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        allow: tuple[int, ...] = (200, 201, 204),
        retries: Optional[int] = None,
        accept: Optional[str] = None,
    ) -> requests.Response:
        """
        调用 GitHub API（带重试与明确的中文错误提示）。

        :param method: HTTP 方法
        :param path: 以 /repos/... 开头的路径
        :param params: 查询参数
        :param json_body: JSON 请求体
        :param allow: 视为成功的状态码
        :param retries: 最大尝试次数，默认读配置
        :param accept: 覆盖 Accept 头（例如取原始 diff 时用 application/vnd.github.diff）
        :return: requests.Response
        :raises ReviewError: 鉴权失败、权限不足、网络多次失败等
        """
        url = path if path.startswith("http") else f"{cfg.GITHUB_API_BASE}{path}"
        attempts = max(1, retries or cfg.GITHUB_MAX_RETRIES)
        last_error: Optional[Exception] = None

        headers = self._headers()
        if accept:
            headers["Accept"] = accept

        for attempt in range(1, attempts + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=cfg.GITHUB_TIMEOUT,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("GitHub 请求异常（第 %d/%d 次）：%s %s -> %s",
                               attempt, attempts, method, url, exc)
                if attempt < attempts:
                    time.sleep(2 * attempt)
                    continue
                raise ReviewError(f"GitHub 请求失败（已重试 {attempts} 次）：{exc}") from exc

            code = response.status_code

            if code in allow:
                return response

            if code == 401:
                raise ReviewError(
                    "GitHub Token 无效或已过期（401）：请检查 GITHUB_TOKEN 是否有效", 401
                )
            if code == 403:
                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining == "0":
                    raise ReviewError("GitHub API 调用次数已用尽（403 限流），请稍后重试", 403)
                raise ReviewError(
                    "GitHub 拒绝访问（403）：Token 缺少 Issues 写权限（Actions 中需 "
                    "permissions: issues: write）",
                    403,
                )
            if code == 404:
                raise ReviewError(
                    f"资源不存在（404）：{path}，请确认仓库 {self.repo} 与编号是否正确", 404
                )
            if code >= 500:
                logger.warning("GitHub 服务端错误 %s（第 %d/%d 次）", code, attempt, attempts)
                if attempt < attempts:
                    time.sleep(2 * attempt)
                    continue
                raise ReviewError(f"GitHub 服务端持续返回 {code}，请稍后重试", code)

            raise ReviewError(
                f"GitHub 返回意外状态码 {code}：{response.text[:200]}", code
            )

        raise ReviewError(f"GitHub 请求失败：{last_error}")

    # ========================================================
    # 提交与 diff
    # ========================================================
    @staticmethod
    def _is_zero_sha(sha: str) -> bool:
        """判断是否为空提交 SHA（新分支首次 push 时 before 全为 0）。"""
        return bool(sha) and set(sha) == {"0"}

    @staticmethod
    def _is_ignored(filename: str) -> bool:
        """判断文件是否无需送审（锁文件、二进制资产等）。"""
        return any(pattern.search(filename or "") for pattern in _IGNORED_PATH_RES)

    def _fetch_changed_files(
        self, before_sha: str, after_sha: str
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        取本次 push 变更的文件列表。

        :param before_sha: push 前的 SHA（新分支时为全 0 或空）
        :param after_sha: push 后的 SHA
        :return: (GitHub 返回的 files 列表, 附加信息字典)
        """
        if not after_sha:
            raise ReviewError("缺少本次提交 SHA（after_sha）")

        meta: dict[str, Any] = {"mode": "compare"}

        if not before_sha or self._is_zero_sha(before_sha) or before_sha == after_sha:
            # 新分支 / 首次提交：只能看这一个提交自身的 diff
            meta["mode"] = "single-commit"
            data = self._request("GET", f"/repos/{self.repo}/commits/{after_sha}").json()
            meta["total_commits"] = 1
            return data.get("files", []) or [], meta

        try:
            data = self._request(
                "GET", f"/repos/{self.repo}/compare/{before_sha}...{after_sha}"
            ).json()
        except ReviewError as exc:
            # before 被 force-push 顶掉时 compare 会 404，退化为只看最后一个提交
            if exc.status_code != 404:
                raise
            logger.warning("compare %s...%s 失败（很可能被 force-push 覆盖），退化为单提交审核",
                           before_sha[:7], after_sha[:7])
            meta["mode"] = "single-commit-fallback"
            data = self._request("GET", f"/repos/{self.repo}/commits/{after_sha}").json()
            meta["total_commits"] = 1
            return data.get("files", []) or [], meta

        meta["total_commits"] = data.get("total_commits", 0)
        meta["compare_status"] = data.get("status", "")
        return data.get("files", []) or [], meta

    def _format_diff(
        self, files: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        """
        把文件列表拼成给 LLM 看的 diff 文本（带体积控制与截断标记）。

        :param files: GitHub 的 files 数组
        :return: (diff 文本, 统计信息)
        """
        blocks: list[str] = []
        used = 0
        skipped: list[str] = []
        missing_patch: list[str] = []
        additions = deletions = 0
        truncated = False

        for index, item in enumerate(files):
            filename = item.get("filename", "")
            status = item.get("status", "modified")
            additions += int(item.get("additions") or 0)
            deletions += int(item.get("deletions") or 0)

            if self._is_ignored(filename):
                skipped.append(filename)
                continue

            patch = item.get("patch")
            if not patch:
                # 注意：这里**不能**直接当成二进制文件跳过。
                # GitHub 对较大的 diff 会省略 files[].patch 字段（文件本身完全正常），
                # 真正的二进制文件也只是没有 patch。把两者混为一谈会导致
                # “明明改了代码却报没有可审核变更”。
                if patch is None:
                    missing_patch.append(filename)
                skipped.append(f"{filename}（未返回 patch）")
                continue

            if len(patch) > cfg.DIFF_MAX_CHARS_PER_FILE:
                patch = patch[: cfg.DIFF_MAX_CHARS_PER_FILE] + "\n... [本文件 diff 已截断]"

            block = (
                f"### {filename}  [{status}, +{item.get('additions', 0)}/-{item.get('deletions', 0)}]\n"
                f"```diff\n{patch}\n```"
            )

            if used + len(block) > cfg.DIFF_MAX_CHARS:
                truncated = True
                blocks.append(
                    f"\n... [已截断：本次共 {len(files)} 个文件，"
                    f"还有 {len(files) - index} 个文件未展示]"
                )
                break

            blocks.append(block)
            used += len(block)

        stats = {
            "files": len(files),
            "reviewed_files": len(blocks),
            "additions": additions,
            "deletions": deletions,
            "skipped": skipped,
            "missing_patch": missing_patch,
            "truncated": truncated,
            "chars": used,
        }
        return "\n\n".join(blocks), stats

    def _fetch_raw_diff(self, before_sha: str, after_sha: str) -> str:
        """
        直接以 ``application/vnd.github.diff`` 媒体类型取原始 unified diff。

        用途：GitHub 在 diff 较大时会省略 ``files[].patch`` 字段，此时只能靠这个接口
        拿到真正的改动内容。找不到时返回空串（不抛异常）。

        :param before_sha: push 前的 SHA（空或全 0 表示单提交）
        :param after_sha: push 后的 SHA
        :return: 原始 diff 文本
        """
        if not before_sha or self._is_zero_sha(before_sha) or before_sha == after_sha:
            path = f"/repos/{self.repo}/commits/{after_sha}"
        else:
            path = f"/repos/{self.repo}/compare/{before_sha}...{after_sha}"

        try:
            response = self._request("GET", path, accept="application/vnd.github.diff")
        except ReviewError as exc:
            logger.warning("获取原始 diff 失败：%s", exc.message)
            return ""

        text = getattr(response, "text", "") or ""
        if not text.strip():
            return ""

        # 原始 diff 可能很大，这里按总量上限截断后再交给模型
        if len(text) > cfg.DIFF_MAX_CHARS:
            head = text[: cfg.DIFF_MAX_CHARS]
            # 尽量切在完整的一行上，避免把一行 diff 劈成两半
            cut = head.rfind("\n")
            if cut > cfg.DIFF_MAX_CHARS // 2:
                head = head[:cut]
            text = head + "\n... [已截断：原始 diff 过长，只展示了前面一部分]"

        logger.info("已改用原始 diff 接口取得 %d 字符的可审核内容", len(text))
        return text

    def _collect_diff(
        self, files: list[dict[str, Any]], before_sha: str, after_sha: str
    ) -> tuple[str, dict[str, Any]]:
        """
        取「可审核的 diff 文本」，带 patch 缺失时的兜底。

        流程：
        1. 先用 files[].patch 拼 diff（常规路径）
        2. 如果拼出来是空的、但确实有文件改动（说明 GitHub 省略了 patch 字段），
           改用 ``application/vnd.github.diff`` 再取一次

        :param files: _fetch_changed_files() 的返回值
        :param before_sha: push 前的 SHA
        :param after_sha: push 后的 SHA
        :return: (diff 文本, 统计信息)
        """
        diff_text, stats = self._format_diff(files)

        if diff_text.strip() or not files:
            return diff_text, stats

        # 全都没 patch 时，先确认“有没有本该被审核的文件”：
        # 如果改的全是二进制/锁文件（本就在忽略名单里），那跳过是正确的，不必再发请求。
        candidates = [f.get("filename", "") for f in files
                      if not self._is_ignored(f.get("filename", ""))]
        if not candidates:
            logger.info("改动全部位于被忽略的文件中（二进制/锁文件），跳过审核")
            return diff_text, stats

        # 有正常文件却拿不到 patch → 几乎可以确定是 GitHub 因体积省略了 patch
        logger.warning("files[].patch 全部缺失（共 %d 个文件，其中 %d 个本该审核：%s），改用原始 diff 接口重试",
                       len(files), len(candidates), ", ".join(candidates[:5]))
        raw = self._fetch_raw_diff(before_sha, after_sha)
        if raw.strip():
            stats = dict(stats)
            stats.update({
                "raw_diff": True,
                "chars": len(raw),
                "reviewed_files": len(candidates),
            })
            notes = "# 以下为原始 unified diff（GitHub 未返回逐文件 patch）\n\n"
            return notes + raw, stats

        return diff_text, stats

    def get_commit_diff(self, before_sha: str, after_sha: str) -> str:
        """
        获取两次提交之间的 diff 文本。

        :param before_sha: push 前的 SHA
        :param after_sha: push 后的 SHA
        :return: 拼接好的 diff 文本（可能被截断）
        """
        files, _ = self._fetch_changed_files(before_sha, after_sha)
        diff_text, stats = self._collect_diff(files, before_sha, after_sha)
        logger.info("diff 统计：%s", {k: v for k, v in stats.items() if k != "skipped"})
        return diff_text

    def get_commit_message(self, commit_sha: str) -> str:
        """
        获取提交信息（完整 message，含正文）。

        :param commit_sha: 提交 SHA
        :return: 提交信息文本
        """
        data = self.get_commit(commit_sha)
        return ((data.get("commit") or {}).get("message") or "").strip()

    def get_commit(self, commit_sha: str) -> dict[str, Any]:
        """
        获取单个提交的详情。

        :param commit_sha: 提交 SHA
        :return: GitHub 返回的提交对象
        """
        return self._request("GET", f"/repos/{self.repo}/commits/{commit_sha}").json()

    # ========================================================
    # 关联 Issue
    # ========================================================
    def get_linked_issues(self, commit_message: str) -> list[int]:
        """
        从提交信息中提取关联的 Issue 编号。

        支持 close/closes/closed、fix/fixes/fixed、resolve/resolves/resolved
        （以及 -ing 形式）加编号，也支持直接写 ``#1``。
        返回顺序为“关键词写法优先、按出现先后”，并已去重。

        :param commit_message: 提交信息
        :return: Issue 编号列表（可能为空）
        """
        text = commit_message or ""
        numbers: list[int] = []

        for match in _KEYWORD_RE.finditer(text):
            numbers.append(int(match.group(1)))
        for match in _BARE_RE.finditer(text):
            numbers.append(int(match.group(1)))

        seen: set[int] = set()
        result: list[int] = []
        for number in numbers:
            if number > 0 and number not in seen:
                seen.add(number)
                result.append(number)

        logger.info("提交信息提取到关联 Issue：%s", result or "无")
        return result

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        """
        获取 Issue 详情。

        :param issue_number: Issue 编号
        :return: Issue 对象
        """
        return self._request("GET", f"/repos/{self.repo}/issues/{issue_number}").json()

    def _build_issue_info(self, issue_numbers: list[int]) -> str:
        """
        汇总关联 Issue 的信息，供提示词使用。

        :param issue_numbers: Issue 编号列表
        :return: 拼接后的文本
        """
        lines: list[str] = []
        for number in issue_numbers[: cfg.MAX_ISSUES_IN_PROMPT]:
            try:
                issue = self.get_issue(number)
            except ReviewError as exc:
                lines.append(f"#{number}: （获取失败：{exc.message}）")
                continue

            if "pull_request" in issue:
                lines.append(f"#{number}: （这是一个 Pull Request，不是 Issue）")
                continue

            body = (issue.get("body") or "").strip().replace("\r\n", "\n")
            labels = ", ".join(item["name"] for item in issue.get("labels", [])) or "无"
            lines.append(
                f"#{issue.get('number')}: {issue.get('title', '')}\n"
                f"当前状态：{issue.get('state')}；标签：{labels}\n"
                f"正文（截断 500 字）：\n{body[:500]}"
            )

        if len(issue_numbers) > cfg.MAX_ISSUES_IN_PROMPT:
            lines.append(f"...（另有 {len(issue_numbers) - cfg.MAX_ISSUES_IN_PROMPT} 个关联 Issue 未展示）")

        return "\n\n".join(lines)

    # ========================================================
    # 写操作
    # ========================================================
    def add_issue_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        """
        在 Issue 下添加评论。

        :param issue_number: Issue 编号
        :param body: 评论内容（Markdown）
        :return: 新建评论对象
        :raises ReviewError: 写入失败
        """
        response = self._request(
            "POST",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            json_body={"body": body},
            allow=(200, 201),
        )
        logger.info("Issue #%s 已提交审核评论", issue_number)
        return response.json()

    def list_issue_comments(self, issue_number: int, limit: int = 100) -> list[dict[str, Any]]:
        """
        列出 Issue 的评论（最多 100 条）。

        :param issue_number: Issue 编号
        :param limit: 每页数量，GitHub 上限 100
        :return: 评论列表
        """
        response = self._request(
            "GET",
            f"/repos/{self.repo}/issues/{issue_number}/comments",
            params={"per_page": min(max(limit, 1), 100)},
        )
        data = response.json()
        return data if isinstance(data, list) else []

    def has_review_comment(self, issue_number: int, commit_sha: str) -> bool:
        """
        判断该提交是否已经审核过（用于 Action 重跑时避免重复评论）。

        :param issue_number: Issue 编号
        :param commit_sha: 提交 SHA
        :return: 是否已存在同一提交的审核评论
        """
        short = (commit_sha or "")[:7]
        if not short:
            return False

        try:
            comments = self.list_issue_comments(issue_number)
        except ReviewError as exc:
            logger.warning("检查历史评论失败（将照常评论）：%s", exc.message)
            return False

        for comment in comments:
            body = comment.get("body") or ""
            if cfg.REVIEW_MARKER_PREFIX in body and f":{short} -->" in body:
                return True
        return False

    def ensure_label(self, name: str, color: str = "0e8a16") -> None:
        """
        确保仓库中存在某个标签（不存在则创建，已存在则忽略）。

        :param name: 标签名
        :param color: 标签颜色（6 位十六进制，不带 #）
        """
        try:
            self._request(
                "POST",
                f"/repos/{self.repo}/labels",
                json_body={"name": name, "color": color},
                allow=(200, 201),
            )
            logger.info("已创建 GitHub 标签：%s", name)
        except ReviewError as exc:
            # 标签已存在时 GitHub 返回 422，属于正常情况
            if exc.status_code == 422:
                return
            raise

    def update_issue_labels(
        self,
        issue_number: int,
        add_labels: Optional[list[str]] = None,
        remove_labels: Optional[list[str]] = None,
    ) -> None:
        """
        更新 Issue 标签（先删后加）。

        :param issue_number: Issue 编号
        :param add_labels: 要添加的标签
        :param remove_labels: 要移除的标签（不存在时忽略）
        :raises ReviewError: 写入失败
        """
        for label in remove_labels or []:
            try:
                self._request(
                    "DELETE",
                    f"/repos/{self.repo}/issues/{issue_number}/labels/{label}",
                    allow=(200, 204),
                )
                logger.info("Issue #%s 已移除标签 %s", issue_number, label)
            except ReviewError as exc:
                # 本来就没有这个标签 → 404，忽略
                if exc.status_code == 404:
                    logger.debug("Issue #%s 本来就没有标签 %s，忽略", issue_number, label)
                    continue
                raise

        if add_labels:
            for label in add_labels:
                self.ensure_label(label)

            self._request(
                "POST",
                f"/repos/{self.repo}/issues/{issue_number}/labels",
                json_body={"labels": list(add_labels)},
                allow=(200, 201),
            )
            logger.info("Issue #%s 已添加标签 %s", issue_number, ", ".join(add_labels))

    def close_issue(self, issue_number: int) -> dict[str, Any]:
        """
        关闭 Issue。

        :param issue_number: Issue 编号
        :return: 更新后的 Issue 对象
        """
        response = self._request(
            "PATCH",
            f"/repos/{self.repo}/issues/{issue_number}",
            json_body={"state": "closed", "state_reason": "completed"},
            allow=(200,),
        )
        logger.info("Issue #%s 已关闭", issue_number)
        return response.json()

    # ========================================================
    # 报告
    # ========================================================
    def _build_report(
        self,
        result: dict[str, Any],
        commit_message: str,
        commit_title: str,
        commit_sha: str,
        stats: dict[str, Any],
    ) -> str:
        """
        生成写回 Issue 的 Markdown 审核报告。

        :param result: LLM 结论（passed/score/comment/suggestions）
        :param commit_message: 完整提交信息
        :param commit_title: 提交信息首行
        :param commit_sha: 提交 SHA
        :param stats: diff 统计
        :return: Markdown 文本
        """
        passed = bool(result.get("passed"))
        suggestions = result.get("suggestions") or []

        if suggestions:
            lines = "\n".join(f"{index}. {item}" for index, item in enumerate(suggestions, 1))
            suggestions_section = f"\n### 💡 改进建议\n\n{lines}\n"
        else:
            suggestions_section = ""

        # 提示词里已让模型控制在 300 字以内，这里再兜一层，避免评论过长
        comment = (result.get("comment") or "").strip() or "（模型未给出评语）"
        if len(comment) > 1500:
            comment = comment[:1500] + "…"

        diff_stat = (
            f"{stats.get('files', 0)} 个文件，"
            f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)}"
        )
        if stats.get("truncated"):
            diff_stat += "（diff 已截断）"
        if stats.get("skipped"):
            diff_stat += f"，跳过 {len(stats['skipped'])} 个文件"

        return REPORT_TEMPLATE.format(
            result_badge="✅ 通过" if passed else "❌ 不通过",
            score=result.get("score", 0),
            pass_score=cfg.PASS_SCORE,
            diff_stat=diff_stat,
            short_sha=(commit_sha or "")[:7],
            commit_title=commit_title or "",
            comment=comment,
            suggestions_section=suggestions_section,
            action_footer=PASSED_FOOTER if passed else FAILED_FOOTER,
            model=getattr(self.llm, "model", "unknown"),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            marker=cfg.review_marker(commit_sha),
        )

    # ========================================================
    # 主流程
    # ========================================================
    def review(
        self,
        before_sha: str,
        after_sha: str,
        *,
        dry_run: Optional[bool] = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        执行一次完整审核。

        :param before_sha: push 前的 SHA（新分支可为空或全 0）
        :param after_sha: push 后的 SHA（本次提交）
        :param dry_run: 演练模式（只打印报告，不写 GitHub）；默认用实例配置
        :param force: 忽略“已审核过”的判断，强制重新评论
        :return: 审核结果摘要字典
        """
        dry = self.dry_run if dry_run is None else dry_run
        short = (after_sha or "")[:7]

        summary: dict[str, Any] = {
            "skipped": False,
            "passed": False,
            "score": 0,
            "issues": [],
            "commit": short,
            "handled": [],
            "error": None,
            "deduped": False,   # True 表示因“已审核过”而短路（没有调用 LLM）
        }

        logger.info("=" * 56)
        logger.info("开始审核提交 %s（%s）", short, "演练模式" if dry else "正式模式")

        # ---- 1. 取变更文件与 diff ----
        files, meta = self._fetch_changed_files(before_sha, after_sha)
        # _collect_diff 内部会在 GitHub 省略 patch 字段时自动改用原始 diff 接口兜底
        diff_text, stats = self._collect_diff(files, before_sha, after_sha)
        summary["stats"] = stats
        summary["mode"] = meta.get("mode")

        if not diff_text.strip():
            if files:
                reason = (f"本次改动共 {len(files)} 个文件，但都取不到可审核的 diff"
                          f"（二进制文件，或原始 diff 也为空）")
            else:
                reason = "本次提交没有文件变更"
            logger.warning(reason)
            summary.update({"skipped": True, "reason": reason})
            return summary

        # ---- 2. 提交信息 ----
        commit_message = self.get_commit_message(after_sha)
        commit_title = commit_message.splitlines()[0] if commit_message else ""
        summary["commit_message"] = commit_title

        # ---- 3. 关联 Issue ----
        issue_numbers = self.get_linked_issues(commit_message)
        if not issue_numbers:
            reason = "提交信息中没有关联 Issue，跳过审核（写法：close #1 / fixes #1 / 直接写 #1）"
            logger.warning(reason)
            summary.update({"skipped": True, "reason": reason})
            return summary
        summary["issues"] = issue_numbers

        # ---- 4. 重复审核短路：同一个提交已经审过就直接结束，**连 LLM 都不调用**（省 token）----
        # 触发场景：Actions 里点了 “Re-run jobs”、同一个提交被重复推送、
        # 或手动 workflow_dispatch 指定了审过的 SHA。
        # 注意：这一步必须放在调 LLM 之前，否则 token 已经花掉了才发现是重复的。
        if not force and not dry:
            already = [number for number in issue_numbers
                       if self.has_review_comment(number, after_sha)]
            if already and len(already) == len(issue_numbers):
                listed = "、".join(f"#{number}" for number in already)
                reason = (f"提交 {short} 已经审核过（{listed} 上已有本次审核报告），"
                          f"本次跳过且不调用 LLM，不消耗 token")
                logger.info(reason)
                summary.update({"skipped": True, "reason": reason, "deduped": True})
                return summary
            if already:
                logger.info("Issue %s 已有提交 %s 的审核报告，本次只处理其余 Issue",
                            already, short)

        # ---- 5. 调 LLM ----
        issue_info = self._build_issue_info(issue_numbers)
        user_prompt = build_user_prompt(commit_message, issue_info, diff_text)

        logger.info("调用 LLM 审核中（diff %d 字符，关联 %s）...",
                    len(diff_text), issue_numbers)
        try:
            result = self.llm.chat_json(SYSTEM_PROMPT, user_prompt)
        except LLMError as exc:
            # 基础设施故障：留一条“审核未完成”的评论，绝不改 Issue 状态
            logger.error("LLM 调用失败：%s", exc.message)
            summary["error"] = exc.message
            result = {
                "passed": False,
                "score": 0,
                "suggestions": [],
                "parse_failed": False,
                "model_passed": None,
                "comment": (
                    f"⚠️ 审核未能完成：{exc.message}\n\n"
                    "本次未对代码做出评价，Issue 状态保持不变。"
                    "请检查 LLM 相关配置（agent/.env 或仓库 Secrets）后重新触发。"
                ),
            }

        summary["passed"] = bool(result.get("passed"))
        summary["score"] = result.get("score", 0)
        summary["parse_failed"] = bool(result.get("parse_failed"))

        logger.info("审核结论：%s（score=%s）",
                    "✅ 通过" if summary["passed"] else "❌ 不通过", summary["score"])

        # ---- 6. 逐个 Issue 处理 ----
        for number in issue_numbers:
            summary["handled"].append(
                self._handle_issue(
                    number,
                    result,
                    commit_message=commit_message,
                    commit_title=commit_title,
                    commit_sha=after_sha,
                    stats=stats,
                    dry_run=dry,
                    force=force,
                )
            )

        return summary

    def _handle_issue(
        self,
        issue_number: int,
        result: dict[str, Any],
        *,
        commit_message: str,
        commit_title: str,
        commit_sha: str,
        stats: dict[str, Any],
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        处理单个 Issue：留评语；过审才改标签为 done 并关闭。

        :param issue_number: Issue 编号
        :param result: LLM 结论
        :param commit_message: 完整提交信息
        :param commit_title: 提交信息首行
        :param commit_sha: 提交 SHA
        :param stats: diff 统计
        :param dry_run: 演练模式
        :param force: 强制重新评论（忽略去重）
        :return: 处理结果字典
        """
        info: dict[str, Any] = {
            "issue": issue_number,
            "commented": False,
            "closed": False,
            "labels_updated": False,
            "skipped": False,
        }

        report = self._build_report(result, commit_message, commit_title, commit_sha, stats)

        # ---- 演练模式：只打印，不碰 GitHub ----
        if dry_run:
            print("\n" + "=" * 60)
            print(f"[演练模式] 将要发布到 Issue #{issue_number} 的评论：")
            print("=" * 60)
            print(report)
            print("=" * 60 + "\n")
            info["dry_run"] = True
            return info

        # ---- 去重：同一个提交不重复评论 ----
        if not force and self.has_review_comment(issue_number, commit_sha):
            logger.info("Issue #%s 已存在提交 %s 的审核评论，跳过", issue_number, commit_sha[:7])
            info.update({"skipped": True, "reason": "该提交已经审核过"})
            return info

        # ---- 无论过不过审都留评语 ----
        self.add_issue_comment(issue_number, report)
        info["commented"] = True

        # ---- 过审才改状态 ----
        if result.get("passed"):
            self.update_issue_labels(
                issue_number,
                add_labels=["done"],
                remove_labels=["todo", "doing"],
            )
            info["labels_updated"] = True
            self.close_issue(issue_number)
            info["closed"] = True
            logger.info("Issue #%s 已标记 done 并关闭", issue_number)
        else:
            logger.info("Issue #%s 保持原状态（仅留评语）", issue_number)

        return info
