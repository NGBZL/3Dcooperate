"""
Agent 自检脚本（无需网络、无需真实 API Key）
--------------------------------------------
验证审核 Agent 的关键逻辑，可随时重跑：

    python -m agent.selftest
    python agent/selftest.py

覆盖内容：
1. 关联 Issue 正则（全部关键词 + 裸引用 + 误报防护）
2. LLM 输出的 JSON 解析（代码块包裹 / 夹带散文 / 字段非法 / 类型错误 / 分数越界 / passed 与 score 矛盾）
3. JSON 解析失败时的降级处理（默认不通过）
4. review() 全流程：过审 → 评论 + 标签 done + 关闭
5. review() 全流程：不过审 → 只留评语，状态不变
6. LLM 调用失败 → 只留“审核未完成”评论，不改状态，退出码非 0
7. 无关联 Issue / 无代码变更 → 跳过
8. 同一提交重复审核 → 评论去重
9. diff 体积控制与忽略文件
10. GitHub 写操作失败（404 标签不存在等）的容错
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# 兼容 python -m agent.selftest 与 python agent/selftest.py
# 注意：这里**故意不导入 agent.run**。
# 一旦 run.py 被写坏（例如混入 NUL 字节），模块级 import 会让本自检直接
# 抛 SyntaxError 而无法给出任何有用提示，所以改成用的时候再延迟导入。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent import config as cfg
    from agent.llm_client import LLMClient, LLMError
    from agent.reviewer import CodeReviewer, ReviewError
else:
    from . import config as cfg
    from .llm_client import LLMClient, LLMError
    from .reviewer import CodeReviewer, ReviewError

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

FAILURES: list[str] = []

# 需要做编码体检的文件类型
SOURCE_SUFFIXES = {
    ".py", ".md", ".js", ".css", ".html", ".yml", ".yaml", ".txt",
    ".json", ".toml", ".cfg", ".ini", ".example",
}
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".idea", ".vscode"}

NUL = bytes([0])
UTF16_BOMS = (bytes([0xFF, 0xFE]), bytes([0xFE, 0xFF]))
UTF8_BOM = bytes([0xEF, 0xBB, 0xBF])


def _load_run_module():
    """
    延迟导入 agent.run。

    :return: agent.run 模块
    """
    if __package__ in (None, ""):
        from agent import run as agent_run
    else:
        from . import run as agent_run
    return agent_run


def _tracked_files(root: Path) -> Optional[list[Path]]:
    """
    用 git 列出仓库里被跟踪的文件。

    :param root: 仓库根目录
    :return: 文件路径列表；不在 git 仓库中（或 git 不可用）时返回 None
    """
    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=str(root), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    return [root / line for line in result.stdout.splitlines() if line.strip()]


def scan_source_encoding(root: Path) -> list[str]:
    """
    体检仓库里源码/文档的编码，重点抓 NUL 字节。

    为什么要有这一步：Windows PowerShell 5.1 里 `>>`、`>`、`Add-Content` 默认按
    **UTF-16LE** 写入，用它们追加/重定向就会在普通文本文件里留下 NUL 字节。
    Python 遇到这种文件会直接报
    ``SyntaxError: source code string cannot contain null bytes``，
    而且报错位置常常是 **import 它的那个文件**（例如 selftest.py），很容易误判。

    检查范围优先用 ``git ls-files``（即 CI 里会看到的那批文件），
    避免把本地的临时输出文件也算进来；不在 git 仓库时退化为目录遍历。

    :param root: 仓库根目录
    :return: 问题描述列表（空列表表示全部正常）
    """
    problems: list[str] = []

    tracked = _tracked_files(root)
    if tracked is not None:
        candidates = tracked
    else:
        candidates = [p for p in sorted(root.rglob("*"))
                      if p.is_file() and not any(part in SKIP_DIRS for part in p.parts)]

    for path in candidates:
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES and path.name != ".gitignore":
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            continue

        rel = path.relative_to(root)
        if NUL in raw:
            problems.append(f"{rel}: 含 {raw.count(NUL)} 个 NUL 字节（文件被写成 UTF-16 了？）")
        elif raw[:2] in UTF16_BOMS:
            problems.append(f"{rel}: 是 UTF-16 编码（带 BOM），应改为 UTF-8")
        elif raw[:3] == UTF8_BOM:
            problems.append(f"{rel}: 含 UTF-8 BOM，建议去掉")

    return problems


def check(name: str, condition: bool, extra: Any = "") -> None:
    """打印一条断言结果。"""
    print(("  PASS  " if condition else "  FAIL  ") + name + (f"  -> {extra}" if not condition and extra != "" else ""))
    if not condition:
        FAILURES.append(name)


# ============================================================
# 测试替身
# ============================================================
class FakeResponse:
    """模拟 requests.Response 的最小实现。"""

    def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        """返回预制 JSON 数据。"""
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeGitHub:
    """
    模拟 GitHub API。

    :param files: compare/commit 接口返回的文件列表
    :param message: 提交信息
    :param existing_comments: Issue 上已有的评论（所有 Issue 共用）
    :param issue_state: Issue 当前状态
    :param reviewed_issues: 只给这些 Issue 返回“已有本次提交报告”的评论，
                            用于测试“部分已审 / 全部已审”的分支
    :param marker_sha: 评论里标记的 commit sha
    :param raw_diff: 以 vnd.github.diff 媒体类型请求时返回的原始 diff
                     （用于模拟“GitHub 省略了 files[].patch”的场景）
    :param missing_issues: 这些 Issue 编号一律返回 404（模拟提交信息里写错编号）
    """

    def __init__(
        self,
        files: list[dict[str, Any]],
        message: str,
        existing_comments: Optional[list[dict[str, Any]]] = None,
        issue_state: str = "open",
        reviewed_issues: Optional[set[int]] = None,
        marker_sha: str = "b" * 40,
        raw_diff: str = "",
        missing_issues: Optional[set[int]] = None,
        pull_request_issues: Optional[set[int]] = None,
    ) -> None:
        self.files = files
        self.message = message
        self.existing_comments = existing_comments or []
        self.issue_state = issue_state
        self.reviewed_issues = reviewed_issues
        self.marker_sha = marker_sha
        self.raw_diff = raw_diff
        self.missing_issues = set(missing_issues or ())
        self.pull_request_issues = set(pull_request_issues or ())
        self.calls: list[dict[str, Any]] = []

    # -- 与真实 _request 保持同样的契约：非 allow 的状态码抛 ReviewError --
    def __call__(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        allow: tuple[int, ...] = (200, 201, 204),
        retries: Optional[int] = None,
        accept: Optional[str] = None,
    ) -> FakeResponse:
        self.calls.append({"method": method, "path": path, "json": json_body, "accept": accept})
        # 请求原始 diff（application/vnd.github.diff）时直接返回预制文本
        if accept and "diff" in accept:
            return FakeResponse(200, None, text=self.raw_diff)
        status, payload = self._route(method, path, json_body)
        if status not in allow:
            raise ReviewError(f"假装 GitHub 返回 {status}：{path}", status)
        return FakeResponse(status, payload)

    def diff_requests(self) -> list[dict[str, Any]]:
        """找出所有「请求原始 diff」的调用。"""
        return [c for c in self.calls if c.get("accept") and "diff" in c["accept"]]

    def _route(
        self, method: str, path: str, json_body: Optional[dict[str, Any]] = None
    ) -> tuple[int, Any]:
        """根据路径分发预制响应。"""
        # 编号写错的场景：这些 Issue 的一切请求都返回 404
        if self.missing_issues and "/issues/" in path:
            try:
                number = int(path.split("/issues/")[1].split("/")[0])
            except (IndexError, ValueError):
                number = -1
            if number in self.missing_issues:
                return 404, {"message": "Not Found"}

        if "/compare/" in path:
            return 200, {"files": self.files, "total_commits": 1, "status": "ahead"}
        if "/commits/" in path:
            return 200, {"commit": {"message": self.message}, "files": self.files}
        if path.endswith("/comments") and method == "GET":
            # reviewed_issues 用于区分“哪个 Issue 已经有本次提交的报告”
            if self.reviewed_issues is not None:
                number = int(path.split("/issues/")[1].split("/")[0])
                if number in self.reviewed_issues:
                    return 200, [{"body": f"历史报告\n{cfg.review_marker(self.marker_sha)}"}]
                return 200, []
            return 200, self.existing_comments
        if path.endswith("/comments") and method == "POST":
            return 201, {"id": 999, "body": (json_body or {}).get("body", "")}
        if "/labels/" in path and method == "DELETE":
            label = path.rsplit("/", 1)[-1]
            # todo 标签本来就不存在 → 404，用于验证容错
            return (404, {"message": "Label does not exist"}) if label == "todo" else (204, None)
        if path.endswith("/labels") and method == "POST":
            # 注意：仓库级 /labels 与 Issue 级 /issues/N/labels 要分开
            if "/issues/" in path:
                return 200, []          # 给 Issue 打标签成功
            return 422, {"message": "Validation Failed"}  # 仓库标签已存在
        if "/issues/" in path and method == "PATCH":
            return 200, {"number": int(path.rsplit("/", 1)[-1]), "state": "closed"}
        if "/issues/" in path and method == "GET":
            number = int(path.rsplit("/", 1)[-1])
            payload: dict[str, Any] = {
                "number": number,
                "title": "完成点云生成模块",
                "body": "**负责人**: 小美",
                "state": self.issue_state,
                "labels": [{"name": "doing"}],
            }
            if number in self.pull_request_issues:
                # GitHub 的 issue 对象里带 pull_request 字段 = 它其实是 PR
                payload["pull_request"] = {"url": f"https://example.test/pulls/{number}"}
            return 200, payload
        raise AssertionError(f"FakeGitHub 未处理的请求：{method} {path}")

    # -- 查询辅助 --
    def called(self, method: str, suffix: str) -> bool:
        """判断是否调用过某个接口。"""
        return any(c["method"] == method and c["path"].endswith(suffix) for c in self.calls)

    def find(self, method: str, contains: str) -> list[dict[str, Any]]:
        """找出匹配的调用记录。"""
        return [c for c in self.calls if c["method"] == method and contains in c["path"]]


class FakeLLM:
    """模拟已经规整好的 LLM 结论（绕过 JSON 解析，用于测 review 流程）。"""

    model = "fake-model"

    def __init__(self, passed: bool = True, score: int = 88,
                 comment: str = "整体不错", suggestions: Optional[list[str]] = None,
                 error: Optional[Exception] = None) -> None:
        self.result = {
            "passed": passed,
            "score": score,
            "comment": comment,
            "suggestions": suggestions if suggestions is not None else ["补充单元测试"],
            "parse_failed": False,
            "model_passed": passed,
        }
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: Any = None) -> dict[str, Any]:
        """返回预制结论或抛出预制异常。"""
        self.calls.append((system_prompt, user_prompt))
        if self.error:
            raise self.error
        return dict(self.result)


class StubChatClient(LLMClient):
    """
    复用真实 chat_json 的解析/降级逻辑，只把 chat() 换成返回固定文本。

    这样可以在不配置 API Key、不发网络请求的前提下测试解析逻辑。
    """

    def __init__(self, content: str) -> None:
        # 故意不调用父类 __init__，避免要求 LLM_API_KEY
        self.content = content
        self.model = "stub-model"
        self.api_url = "https://example.invalid/v1/chat/completions"
        self.api_key = "stub"
        self.timeout = 1

    def chat(self, system_prompt: str, user_prompt: str, temperature: Any = None) -> str:
        """直接返回预制文本。"""
        return self.content


def make_diff_file(name: str = "recon/pointcloud.py", patch: str = "@@ -1,3 +1,4 @@\n+import numpy as np\n") -> dict[str, Any]:
    """构造一条 GitHub files 记录。"""
    return {
        "filename": name,
        "status": "modified",
        "additions": 1,
        "deletions": 0,
        "patch": patch,
    }


def make_reviewer(fake: FakeGitHub, llm: Any, **kwargs: Any) -> CodeReviewer:
    """构造一个把 _request 换成假实现的 CodeReviewer。"""
    reviewer = CodeReviewer(github_token="fake-token", repo="demo/repo", llm=llm, **kwargs)
    reviewer._request = fake  # 实例属性覆盖，避免任何真实网络请求
    return reviewer


# ============================================================
# 1. 关联 Issue 正则
# ============================================================
def test_linked_issues() -> None:
    """覆盖需求列出的全部关键词写法。"""
    print("[1] 关联 Issue 正则提取")

    fake = FakeGitHub([make_diff_file()], "x")
    reviewer = make_reviewer(fake, FakeLLM())

    cases = [
        ("close #1", [1]),
        ("closes #1", [1]),
        ("closed #1", [1]),
        ("Close: #12", [12]),
        ("fix #2", [2]),
        ("fixes #2", [2]),
        ("fixed #2", [2]),
        ("resolve #3", [3]),
        ("resolves #3", [3]),
        ("resolved #3", [3]),
        ("closing #4", [4]),
        ("fixing #5", [5]),
        ("resolving #6", [6]),
        ("直接写 #7", [7]),
        ("feat: 完成点云生成模块 #1", [1]),
        ("fix: 修复崩溃\n\n补充说明\n\nCloses #9", [9]),
        ("fixes #1 and resolves #2", [1, 2]),
        ("close #1, close #1", [1]),                 # 去重
        ("FIXES #1", [1]),                            # 大小写不敏感
        ("prefixes 12 不应命中", []),                  # 词边界防护
        ("update readme", []),                        # 无引用
        ("颜色 #123456 不应命中", []),                 # 6 位色值防护
        ("v1.2.3 版本发布", []),                       # 纯数字
        ("close #1 #2", [1, 2]),                      # 关键词 + 裸引用
    ]

    for message, expected in cases:
        got = reviewer.get_linked_issues(message)
        check(f"「{message}」-> {expected}", got == expected, got)

    # 顺序：关键词写法优先于裸引用
    check("关键词优先于裸引用",
          reviewer.get_linked_issues("顺便 #9，主要 fixes #3") == [3, 9],
          reviewer.get_linked_issues("顺便 #9，主要 fixes #3"))


# ============================================================
# 2. JSON 解析与降级
# ============================================================
def test_json_parsing() -> None:
    """测试 chat_json 的解析、校验与降级。"""
    print("[2] LLM 输出解析与降级")

    # 2.1 标准 JSON
    client = StubChatClient('{"passed": true, "score": 88, "comment": "不错", "suggestions": ["加测试"]}')
    result = client.chat_json("s", "u")
    check("标准 JSON 解析", result["passed"] is True and result["score"] == 88
          and result["comment"] == "不错" and result["suggestions"] == ["加测试"], result)

    # 2.2 ```json 代码块包裹
    client = StubChatClient('```json\n{"passed": false, "score": 55, "comment": "要改", "suggestions": []}\n```')
    result = client.chat_json("s", "u")
    check("代码块包裹可解析", result["score"] == 55 and result["passed"] is False, result)

    # 2.3 夹带散文
    client = StubChatClient('好的，以下是审核结果：\n{"passed": true, "score": 75, "comment": "ok", "suggestions": []}\n以上。')
    result = client.chat_json("s", "u")
    check("夹带散文可解析", result["score"] == 75 and result["passed"] is True, result)

    # 2.4 完全不是 JSON → 降级不通过
    client = StubChatClient("这次提交整体还可以，建议补充测试。")
    result = client.chat_json("s", "u")
    check("非法 JSON 降级为不通过", result["passed"] is False and result["score"] == 0, result)
    check("降级结果带 parse_failed 标记", result["parse_failed"] is True, result)
    check("降级结果含 suggestions", isinstance(result["suggestions"], list) and result["suggestions"], result)
    check("降级评语含原始输出片段", "原始输出片段" in result["comment"], result["comment"])

    # 2.5 缺 score → 降级
    client = StubChatClient('{"passed": true, "comment": "没有分数"}')
    result = client.chat_json("s", "u")
    check("缺 score 降级为不通过", result["passed"] is False and result["parse_failed"] is True, result)

    # 2.6 score 是字符串
    client = StubChatClient('{"passed": true, "score": "88", "comment": "x", "suggestions": []}')
    result = client.chat_json("s", "u")
    check("字符串 score 可解析", result["score"] == 88 and result["passed"] is True, result)

    # 2.7 score 越界 → 夹到 100
    client = StubChatClient('{"passed": true, "score": 150, "comment": "x"}')
    result = client.chat_json("s", "u")
    check("score 越界夹到 100", result["score"] == 100 and result["passed"] is True, result)

    # 2.8 passed 与 score 矛盾 → 以 score 为准
    client = StubChatClient('{"passed": true, "score": 40, "comment": "x"}')
    result = client.chat_json("s", "u")
    check("passed=true 但 score=40 → 判不通过", result["passed"] is False, result)
    check("记录了模型原始 passed", result["model_passed"] is True, result)

    client = StubChatClient('{"passed": false, "score": 90, "comment": "x"}')
    result = client.chat_json("s", "u")
    check("passed=false 但 score=90 → 判通过", result["passed"] is True, result)

    # 2.9 边界：正好 70 分通过
    client = StubChatClient('{"passed": false, "score": 70, "comment": "x"}')
    result = client.chat_json("s", "u")
    check("score=70 恰好通过（>=70）", result["passed"] is True, result)

    # 2.10 suggestions 是字符串 / 空值
    client = StubChatClient('{"passed": true, "score": 80, "comment": "x", "suggestions": "只有一条"}')
    result = client.chat_json("s", "u")
    check("suggestions 字符串转列表", result["suggestions"] == ["只有一条"], result)


# ============================================================
# 3. review() 全流程
# ============================================================
def test_review_passed() -> None:
    """过审：留评语 + 标签 done + 关闭 Issue。"""
    print("[3] review() 过审流程")
    fake = FakeGitHub([make_diff_file()], "feat: 完成点云生成模块\n\ncloses #1")
    reviewer = make_reviewer(fake, FakeLLM(passed=True, score=88))

    result = reviewer.review("a" * 40, "b" * 40)

    check("未跳过", result["skipped"] is False, result)
    check("判定通过", result["passed"] is True and result["score"] == 88, result)
    check("关联到 Issue #1", result["issues"] == [1], result["issues"])
    check("发布了评论", fake.called("POST", "/comments"), fake.calls)
    check("添加了 done 标签", any(c["json"] == {"labels": ["done"]} for c in fake.find("POST", "/labels")), fake.calls)
    check("移除了 doing 标签", any("doing" in c["path"] for c in fake.find("DELETE", "/labels/")), fake.calls)
    check("关闭了 Issue", fake.called("PATCH", "/issues/1"), fake.calls)
    check("处理结果标记已关闭", result["handled"][0]["closed"] is True, result["handled"])

    comment = [c for c in fake.find("POST", "/comments")][0]["json"]["body"]
    check("报告含标题", "Agent 代码审核报告" in comment)
    check("报告含评分与通过线", "88 / 100" in comment and str(cfg.PASS_SCORE) in comment)
    check("报告含评语", "整体不错" in comment)
    check("报告含改进建议", "补充单元测试" in comment)
    check("报告含通过后的动作说明", "标记为 `done` 并关闭" in comment)
    check("报告含去重标记", cfg.review_marker("b" * 40) in comment, comment[-120:])
    check("报告含提交标题", "完成点云生成模块" in comment)

    # 提示词里应该带上 diff 与 Issue 信息
    prompt = reviewer.llm.calls[0][1]
    check("提示词含 diff", "pointcloud.py" in prompt, prompt[:200])
    check("提示词含关联 Issue 信息", "#1" in prompt and "完成点云生成模块" in prompt)


def test_review_no_close() -> None:
    """PR 场景：allow_close=False 时只留评语，不打 done、不关 Issue。"""
    print("[3b] review() PR 模式：只审核不定案")
    fake = FakeGitHub([make_diff_file()], "feat: 完成点云生成模块\n\ncloses #1")
    reviewer = make_reviewer(fake, FakeLLM(passed=True, score=95))

    result = reviewer.review("a" * 40, "b" * 40, allow_close=False)

    check("未跳过", result["skipped"] is False, result)
    check("判定通过", result["passed"] is True, result)
    check("仍然发布了评论", fake.called("POST", "/comments"), fake.calls)
    check("没有添加 done 标签",
          not any(c["json"] == {"labels": ["done"]} for c in fake.find("POST", "/labels")), fake.calls)
    check("没有关闭 Issue", not fake.called("PATCH", "/issues/1"), fake.calls)
    check("处理结果标记为「延后定案」", result["handled"][0].get("deferred") is True,
          result["handled"])
    check("summary 里记录了 allow_close=False", result.get("allow_close") is False, result)
    check("报告里说明等合并后再标记完成",
          "合并" in [c for c in fake.find("POST", "/comments")][0]["json"]["body"], "")


def test_pr_number_ignored() -> None:
    """squash 合并带来的 “(#1)” 是 PR 编号：要当成 PR 忽略，不能去评论它。"""
    print("[3c] review() 关联到 PR 编号时跳过（不调用 LLM）")
    fake = FakeGitHub([make_diff_file()], "test: 自检文件 (#1)", pull_request_issues={1})
    reviewer = make_reviewer(fake, FakeLLM(passed=True, score=90))

    result = reviewer.review("a" * 40, "b" * 40)

    check("跳过了审核", result["skipped"] is True, result)
    check("跳过原因说明了是 PR", "Pull Request" in (result.get("reason") or ""), result.get("reason"))
    check("识别出 #1 是 PR", result.get("pr_numbers") == [1], result.get("pr_numbers"))
    check("没有调用 LLM（没花钱）", reviewer.llm.calls == [], reviewer.llm.calls)
    check("没有给 PR 评论", not fake.called("POST", "/comments"), fake.calls)


def test_review_failed() -> None:
    """不过审：只留评语，状态不变。"""
    print("[4] review() 不过审流程")
    fake = FakeGitHub([make_diff_file()], "fix: 修复崩溃\n\nfixes #2")
    reviewer = make_reviewer(fake, FakeLLM(passed=False, score=45, suggestions=["修复空指针", "补充测试"]))

    result = reviewer.review("a" * 40, "b" * 40)

    check("判定不通过", result["passed"] is False and result["score"] == 45, result)
    check("仍然发布了评论", fake.called("POST", "/comments"), fake.calls)
    check("没有添加 done 标签", not fake.find("POST", "/labels"), fake.find("POST", "/labels"))
    check("没有关闭 Issue", not fake.called("PATCH", "/issues/2"), fake.calls)
    check("处理结果未标记关闭", result["handled"][0]["closed"] is False, result["handled"])

    comment = fake.find("POST", "/comments")[0]["json"]["body"]
    check("报告显示不通过", "❌ 不通过" in comment)
    check("报告含改进建议两条", "修复空指针" in comment and "补充测试" in comment)
    check("报告提示状态不变", "状态保持不变" in comment)


def test_review_llm_error() -> None:
    """LLM 调用失败：留“审核未完成”评论，不改状态，summary 带 error。"""
    print("[5] LLM 调用失败的处理")
    fake = FakeGitHub([make_diff_file()], "feat: x\n\n#3")
    reviewer = make_reviewer(fake, FakeLLM(error=LLMError("LLM_API_KEY 无效或已过期（401）", 401)))

    result = reviewer.review("a" * 40, "b" * 40)

    check("未跳过", result["skipped"] is False, result)
    check("结果带 error", "401" in (result.get("error") or ""), result.get("error"))
    check("判定为不通过", result["passed"] is False, result)
    check("留了审核未完成的评论", fake.called("POST", "/comments"), fake.calls)
    check("没有关闭 Issue", not fake.called("PATCH", "/issues/3"), fake.calls)
    check("没有改标签", not fake.find("POST", "/labels"), fake.find("POST", "/labels"))

    comment = fake.find("POST", "/comments")[0]["json"]["body"]
    check("评论说明审核未完成", "审核未能完成" in comment)


def test_review_paths() -> None:
    """跳过、去重、diff 体积控制、标签容错。"""
    print("[6] 跳过 / 去重 / diff 控制 / 容错")

    # 6.1 没有关联 Issue → 跳过，且不评论
    fake = FakeGitHub([make_diff_file()], "chore: 随便改改")
    reviewer = make_reviewer(fake, FakeLLM())
    result = reviewer.review("a" * 40, "b" * 40)
    check("无关联 Issue 跳过", result["skipped"] is True and "关联 Issue" in result["reason"], result)
    check("跳过时不评论", not fake.called("POST", "/comments"), fake.calls)

    # 6.2 只有二进制/被忽略文件 → 跳过
    fake = FakeGitHub([
        {"filename": "data/scan.pcd", "patch": None, "additions": 0, "deletions": 0},
        {"filename": "package-lock.json", "patch": "@@ -1 +1 @@\n-x\n+y\n"},
    ], "feat: x\n\n#1")
    reviewer = make_reviewer(fake, FakeLLM())
    result = reviewer.review("a" * 40, "b" * 40)
    check("无有效 diff 跳过", result["skipped"] is True and "取不到可审核的 diff" in result["reason"], result)

    # 6.3 已审核过（评论里已有同一 sha 的标记）→ 短路，连 LLM 都不调用
    fake = FakeGitHub(
        [make_diff_file()], "feat: x\n\n#1",
        existing_comments=[{"body": f"历史报告\n{cfg.review_marker('b' * 40)}"}],
    )
    llm = FakeLLM()
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("重复提交直接跳过（deduped）", result["skipped"] is True and result["deduped"] is True, result)
    check("重复提交不调用 LLM（省 token）", llm.calls == [], len(llm.calls))
    check("重复提交没有再次评论", not fake.called("POST", "/comments"), fake.calls)

    # 6.4 --force 强制重审（此时会重新调用 LLM）
    fake = FakeGitHub(
        [make_diff_file()], "feat: x\n\n#1",
        existing_comments=[{"body": f"历史报告\n{cfg.review_marker('b' * 40)}"}],
    )
    llm = FakeLLM()
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40, force=True)
    check("force 时强制评论", result["handled"][0]["commented"] is True, result["handled"])
    check("force 时会调用 LLM", len(llm.calls) == 1, len(llm.calls))

    # 6.5 diff 截断
    original = cfg.DIFF_MAX_CHARS
    cfg.DIFF_MAX_CHARS = 500
    try:
        files = [make_diff_file(f"src/file_{i}.py", "@@ -1 +1 @@\n" + "+x" * 200 + "\n") for i in range(10)]
        reviewer = make_reviewer(FakeGitHub(files, "x"), FakeLLM())
        text, stats = reviewer._format_diff(files)
        check("超长 diff 被截断", stats["truncated"] is True and "已截断" in text, stats)
        check("截断后长度受控", len(text) < 1500, len(text))
    finally:
        cfg.DIFF_MAX_CHARS = original

    # 6.6 单文件超长 → 单文件截断标记
    original_per = cfg.DIFF_MAX_CHARS_PER_FILE
    cfg.DIFF_MAX_CHARS_PER_FILE = 100
    try:
        reviewer = make_reviewer(FakeGitHub([], "x"), FakeLLM())
        text, stats = reviewer._format_diff([make_diff_file(patch="+y" * 500)])
        check("单文件超长被截断", "本文件 diff 已截断" in text, text[:200])
    finally:
        cfg.DIFF_MAX_CHARS_PER_FILE = original_per

    # 6.7 标签接口容错：仓库标签创建返回 422、删除 todo 返回 404 都不应报错
    fake = FakeGitHub([make_diff_file()], "feat: x\n\n#1")
    reviewer = make_reviewer(fake, FakeLLM(passed=True, score=90))
    try:
        reviewer.update_issue_labels(1, add_labels=["done"], remove_labels=["todo", "doing"])
        check("标签已存在(422)/不存在(404) 都能容错", True)
    except ReviewError as exc:
        check("标签已存在(422)/不存在(404) 都能容错", False, exc)

    # 6.8 演练模式不写 GitHub
    fake = FakeGitHub([make_diff_file()], "feat: x\n\n#1")
    reviewer = make_reviewer(fake, FakeLLM(), dry_run=True)
    result = reviewer.review("a" * 40, "b" * 40)
    check("演练模式未写 GitHub", not fake.called("POST", "/comments") and not fake.called("PATCH", "/issues/1"),
          fake.calls)
    check("演练模式标记 dry_run", result["handled"][0].get("dry_run") is True, result["handled"])


def test_token_cost() -> None:
    """
    Token 消耗模型：确认「不该调 LLM 的时候一次都不调」「该调的时候只调一次」。

    Agent 只在 push 到 main 时被 Actions 唤起，这里是它内部的花钱边界：
    空 diff、没有关联 Issue、已审核过 → 0 次调用；
    一次 push 关联多个 Issue → 也只调用 1 次（而不是每个 Issue 一次）。
    """
    print("[7] Token 消耗边界（省钱验证）")

    # 8.1 没有关联 Issue → 不调用 LLM
    llm = FakeLLM()
    reviewer = make_reviewer(FakeGitHub([make_diff_file()], "chore: 随便改改"), llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("无关联 Issue 时 0 次 LLM 调用", llm.calls == [], len(llm.calls))
    check("无关联 Issue 时标记跳过", result["skipped"] is True, result)

    # 8.2 没有可审核的代码变更 → 不调用 LLM
    llm = FakeLLM()
    reviewer = make_reviewer(FakeGitHub([
        {"filename": "data/scan.pcd", "patch": None, "additions": 0, "deletions": 0},
    ], "feat: x\n\n#1"), llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("无有效 diff 时 0 次 LLM 调用", llm.calls == [], len(llm.calls))

    # 8.3 该提交已审核过（Actions 重跑 / 重复推送）→ 不调用 LLM
    llm = FakeLLM()
    reviewer = make_reviewer(
        FakeGitHub([make_diff_file()], "feat: x\n\n#1", reviewed_issues={1}), llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("已审核过的提交 0 次 LLM 调用", llm.calls == [], len(llm.calls))
    check("已审核过时标记为 deduped", result.get("deduped") is True, result)

    # 8.4 一次 push 关联 3 个 Issue → 只调用 1 次
    llm = FakeLLM()
    reviewer = make_reviewer(
        FakeGitHub([make_diff_file()], "feat: x\n\n#1 #2 #3", reviewed_issues=set()), llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("3 个 Issue 也只调用 1 次 LLM", len(llm.calls) == 1, len(llm.calls))
    check("3 个 Issue 都被处理", len(result["handled"]) == 3, len(result["handled"]))
    check("3 个 Issue 都留了评论",
          all(item["commented"] for item in result["handled"]), result["handled"])

    # 8.5 部分已审（#1 已审、#2 未审）→ 仍要审 1 次，但 #1 不重复评论
    llm = FakeLLM()
    fake = FakeGitHub([make_diff_file()], "feat: x\n\n#1 #2", reviewed_issues={1})
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("部分已审时调用 1 次 LLM", len(llm.calls) == 1, len(llm.calls))
    handled = {item["issue"]: item for item in result["handled"]}
    check("已审过的 #1 不重复评论", handled[1]["skipped"] is True, handled[1])
    check("未审的 #2 正常评论", handled[2]["commented"] is True, handled[2])

    # 8.6 演练模式不做去重短路（人工发起的真审）
    llm = FakeLLM()
    reviewer = make_reviewer(
        FakeGitHub([make_diff_file()], "feat: x\n\n#1", reviewed_issues={1}), llm, dry_run=True)
    reviewer.review("a" * 40, "b" * 40)
    check("--dry-run 仍会调用 LLM（预期行为）", len(llm.calls) == 1, len(llm.calls))


def test_missing_patch_fallback() -> None:
    """
    GitHub 省略 files[].patch 时的兜底。

    真实事故：用户 push 只改了 README.md（+596/-594），compare 接口返回了文件但
    **没有 patch 字段**（diff 较大时 GitHub 会省略），旧代码把“没有 patch”当成
    “二进制文件”直接跳过，于是 Actions 显示“没有可审核的代码变更”。
    """
    print("[8] patch 缺失时的兜底（真实事故回归）")

    raw = ("diff --git a/README.md b/README.md\n"
           "--- a/README.md\n"
           "+++ b/README.md\n"
           "@@ -1,2 +1,3 @@\n"
           " # 项目看板\n"
           "+新增的一行说明\n")

    # 9.1 有正常文件但没 patch，且原始 diff 能取到 → 必须照常审核
    files = [{"filename": "README.md", "status": "modified",
              "additions": 596, "deletions": 594, "patch": None}]
    fake = FakeGitHub(files, "test: 测试Agent审核流程 #1", reviewed_issues=set(), raw_diff=raw)
    llm = FakeLLM(passed=True, score=80)
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("1d3bcd2", "af24305")

    check("patch 缺失时不再误判为跳过", result.get("skipped") is False, result)
    check("patch 缺失时确实调用了 LLM（1 次）", len(llm.calls) == 1, len(llm.calls))
    check("提示词里带上了原始 diff", "新增的一行说明" in llm.calls[0][1], llm.calls[0][1][:160])
    check("统计里标记 raw_diff", result["stats"].get("raw_diff") is True, result["stats"])
    check("确实发起了 diff 媒体类型请求", len(fake.diff_requests()) == 1, fake.diff_requests())
    check("跳过文件列表里注明“未返回 patch”",
          any("未返回 patch" in item for item in result["stats"]["skipped"]), result["stats"]["skipped"])

    # 9.2 原始 diff 也拿不到 → 才跳过，且原因要说清楚
    fake = FakeGitHub(files, "test: x #1", raw_diff="")
    llm = FakeLLM()
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("1d3bcd2", "af24305")
    check("都取不到 diff 时才跳过", result.get("skipped") is True, result)
    check("都取不到时不调用 LLM", llm.calls == [], len(llm.calls))
    check("跳过原因写明取不到 diff", "取不到可审核的 diff" in result["reason"], result["reason"])

    # 9.3 全是二进制/锁文件 → 跳过，且不该多发那一次 diff 请求
    binfiles = [{"filename": "data/scan.pcd", "status": "added",
                 "additions": 0, "deletions": 0, "patch": None},
                {"filename": "package-lock.json", "status": "modified",
                 "additions": 9, "deletions": 9, "patch": None}]
    fake = FakeGitHub(binfiles, "feat: x #1", raw_diff=raw)
    llm = FakeLLM()
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("纯二进制/锁文件仍然跳过", result.get("skipped") is True, result)
    check("纯二进制时不额外请求原始 diff", fake.diff_requests() == [], fake.diff_requests())

    # 9.4 常规路径：有 patch 时不应触发兜底（省一次请求）
    normal = [{"filename": "recon/a.py", "status": "modified", "additions": 1,
               "deletions": 0, "patch": "@@ -1 +1,2 @@\n import os\n+import sys\n"}]
    fake = FakeGitHub(normal, "feat: x #1", reviewed_issues=set(), raw_diff=raw)
    llm = FakeLLM()
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40)
    check("有 patch 时不触发兜底请求", fake.diff_requests() == [], fake.diff_requests())
    check("有 patch 时正常审核", result.get("skipped") is False and len(llm.calls) == 1, result)


def test_bad_issue_number() -> None:
    """
    提交信息里写了不存在的 Issue 编号时，不能连累其它 Issue。

    真实场景：`git commit -m "fix: 修好了 #99"` 但 #99 不存在（或是个 PR）。
    旧代码会在花掉 token 之后整个运行失败，其它正常 Issue 连评论都拿不到。
    """
    print("[9] 编号写错时的容错")

    # 9.1 #1 正常、#99 不存在
    fake = FakeGitHub([make_diff_file()], "feat: x\n\n#1 #99",
                      reviewed_issues=set(), missing_issues={99})
    llm = FakeLLM(passed=True, score=80)
    reviewer = make_reviewer(fake, llm)
    result = reviewer.review("a" * 40, "b" * 40)

    check("整体没有崩", result.get("skipped") is False, result)
    check("仍然只调用 1 次 LLM", len(llm.calls) == 1, len(llm.calls))
    handled = {item["issue"]: item for item in result["handled"]}
    check("正常的 #1 照常评论", handled[1]["commented"] is True, handled[1])
    check("正常的 #1 照常改标签并关闭",
          handled[1]["labels_updated"] is True and handled[1]["closed"] is True, handled[1])
    check("#99 被记为失败", [f["issue"] for f in result["failed_issues"]] == [99], result["failed_issues"])
    check("#99 不会留下评论/状态变更",
          handled[99]["commented"] is False and handled[99]["closed"] is False, handled[99])
    check("失败项带上原因", "404" in handled[99].get("error", ""), handled[99])
    check("提示词里说明了 #99 获取失败", "#99" in llm.calls[0][1], llm.calls[0][1][:200])
    check("#1 仍然收到了评论", fake.called("POST", "/issues/1/comments"), fake.calls)

    # 9.2 run.py 的摘要要提醒这件事，并让 Actions 变红
    agent_run = _load_run_module()
    markdown = agent_run._build_summary_markdown(result)
    check("摘要提醒 Issue 处理失败", "处理失败" in markdown and "#99" in markdown, markdown[-200:])


# ============================================================
# 10. 入口脚本
# ============================================================


def test_run_entry() -> None:
    """测试 run.main 的退出码与摘要生成。"""
    print("[10] 入口脚本 run.main")

    # 延迟导入：即使 agent/run.py 被写坏，前面的编码体检也能先给出清晰提示
    agent_run = _load_run_module()

    summary_file = Path(__file__).resolve().parent / "_selftest_summary.md"
    if summary_file.exists():
        summary_file.unlink()

    class StubReviewer:
        """替代真实 CodeReviewer，避免要求 LLM_API_KEY。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.dry_run = kwargs.get("dry_run", False)

        def review(self, before: str, after: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "skipped": False,
                "passed": True,
                "score": 88,
                "issues": [1],
                "commit": after[:7],
                "commit_message": "feat: x",
                "stats": {"files": 1, "additions": 2, "deletions": 1},
                "handled": [{"issue": 1, "commented": True, "labels_updated": True, "closed": True}],
                "error": None,
            }

    original_reviewer = agent_run.CodeReviewer
    original_summary_env = os.environ.get("GITHUB_STEP_SUMMARY")

    # 关键：**整个测试期间**都把 GITHUB_STEP_SUMMARY 指向临时文件。
    # 否则在 GitHub Actions 里（该变量是真实路径），下面“缺少 SHA”那次调用会把
    # 「❌ Agent 审核失败 / 缺少本次提交 SHA」写进真实 Job Summary，
    # 让人误以为是 workflow 没传 COMMIT_SHA —— 这个坑真实发生过，别再踩。
    os.environ["GITHUB_STEP_SUMMARY"] = str(summary_file)
    agent_run.CodeReviewer = StubReviewer  # type: ignore[assignment]
    try:
        code = agent_run.main(["--after", "b" * 40, "--before", "a" * 40, "--repo", "demo/repo"])
        check("成功时退出码 0", code == 0, code)
        check("写入了 Job Summary", summary_file.exists(), summary_file)
        if summary_file.exists():
            summary = summary_file.read_text(encoding="utf-8")
            check("摘要含结论", "✅ 通过" in summary and "88" in summary, summary[:200])
            check("摘要含 Issue 处理表", "| #1 |" in summary, summary)

        # 缺少 after_sha → 退出码 2；这条提示也必须落进临时摘要，而不是真实摘要
        summary_file.unlink(missing_ok=True)
        # 这一次调用是**故意**用空 SHA 触发报错的，把日志压掉，
        # 免得 CI 日志里出现 “缺少本次提交 SHA” 让人误以为是 workflow 没配好
        run_logger = logging.getLogger("agent.run")
        previous_level = run_logger.level
        run_logger.setLevel(logging.CRITICAL)
        try:
            code = agent_run.main(["--after", "", "--before", "a" * 40])
        finally:
            run_logger.setLevel(previous_level)
        check("缺少 commit SHA 返回 2", code == 2, code)
        leaked = summary_file.read_text(encoding="utf-8") if summary_file.exists() else ""
        check("缺 SHA 的提示只写进临时摘要（不污染真实 Job Summary）",
              "缺少本次提交 SHA" in leaked, leaked[:120] or "临时摘要为空")
    finally:
        agent_run.CodeReviewer = original_reviewer  # type: ignore[assignment]
        if original_summary_env is None:
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
        else:
            os.environ["GITHUB_STEP_SUMMARY"] = original_summary_env
        if summary_file.exists():
            summary_file.unlink()


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    """依次执行所有自检。"""
    print("=" * 60)
    print("Agent 自检开始（全部离线，不需要 API Key）")
    print("=" * 60)

    # ---- [0] 编码体检放最前面 ----
    # 文件一旦混入 NUL 字节，后面任何 import 都会以难以定位的 SyntaxError 失败，
    # 所以先单独拦一次，把话说清楚。
    repo_root = Path(__file__).resolve().parent.parent
    encoding_problems = scan_source_encoding(repo_root)
    if encoding_problems:
        print("[0] 源码编码体检 —— 发现问题：")
        for item in encoding_problems:
            print("   ✗", item)
        print()
        print("最常见原因：在 Windows PowerShell 里用 `>>` 或 Add-Content 追加内容，")
        print("默认写成 UTF-16LE，会在文件里留下 NUL 字节。请改用编辑器修改，或：")
        print('   Add-Content -Encoding utf8 <文件> "要追加的内容"')
        print()
        print("修复后重新运行：python -m agent.selftest")
        return 1
    print("[0] 源码编码体检通过（无 NUL 字节 / 无 BOM）")
    print()

    test_linked_issues()
    test_json_parsing()
    test_review_passed()
    test_review_no_close()
    test_pr_number_ignored()
    test_review_failed()
    test_review_llm_error()
    test_review_paths()
    test_token_cost()
    test_missing_patch_fallback()
    test_bad_issue_number()
    test_run_entry()

    print()
    print("=" * 60)
    if FAILURES:
        print(f"[FAILED] 共 {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print("  -", item)
        return 1
    print("✅ 全部自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
