"""
Agent 入口脚本
--------------
既作为 GitHub Actions 的执行入口（python -m agent.run），
也可以在本机手动跑（python agent/run.py --before <sha> --after <sha> --dry-run）。

参数来源优先级：命令行参数 > 环境变量 > .env 文件。

常用环境变量：
    GITHUB_TOKEN / GH_TOKEN        写 Issue、关 Issue 用（Actions 自动注入）
    REPO / GITHUB_REPOSITORY       仓库全名 owner/repo
    BEFORE_SHA                     push 前的 SHA（github.event.before）
    COMMIT_SHA / GITHUB_SHA        push 后的 SHA（github.sha）
    LLM_API_KEY / LLM_API_URL / LLM_MODEL  大模型配置
    AGENT_DRY_RUN=1                演练模式，不写 GitHub

退出码：
    0 —— 审核完成，或按规则跳过（无关联 Issue / 无代码变更）
    1 —— 基础设施失败（配置缺失、GitHub/LLM 调用失败、JSON 解析降级不算失败）
    2 —— 参数缺失（没有拿到 commit SHA）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

# 兼容两种运行方式：python -m agent.run / python agent/run.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.config import env, env_bool, github_repo, github_token, mask_secret
    from agent.llm_client import LLMError
    from agent.reviewer import CodeReviewer, ReviewError
else:
    from .config import env, env_bool, github_repo, github_token, mask_secret
    from .llm_client import LLMError
    from .reviewer import CodeReviewer, ReviewError

logger = logging.getLogger("agent.run")


def _write_step_summary(markdown: str) -> None:
    """
    把 Markdown 摘要追加到 GitHub Actions 的 Job Summary。

    非 Actions 环境下 GITHUB_STEP_SUMMARY 不存在，静默跳过。

    :param markdown: Markdown 文本
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")
    except OSError as exc:
        logger.warning("写入 Job Summary 失败：%s", exc)


def _build_summary_markdown(result: dict[str, Any]) -> str:
    """
    把审核结果渲染成 Actions 页面上的摘要。

    :param result: reviewer.review() 的返回值
    :return: Markdown 文本
    """
    if result.get("skipped"):
        return f"## ⏭️ Agent 审核已跳过\n\n{result.get('reason', '')}\n"

    lines = [
        "## 🤖 Agent 代码审核结果",
        "",
        f"- 提交：`{result.get('commit', '')}` {result.get('commit_message', '')}",
        f"- 结论：{'✅ 通过' if result.get('passed') else '❌ 不通过'}（score = {result.get('score')}）",
        f"- 关联 Issue：{', '.join('#' + str(n) for n in result.get('issues', [])) or '无'}",
    ]

    stats = result.get("stats") or {}
    if stats:
        lines.append(
            f"- 变更规模：{stats.get('files', 0)} 个文件，"
            f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)}"
        )

    if result.get("error"):
        lines += ["", f"> ⚠️ 审核未能完成：{result['error']}"]

    handled = result.get("handled") or []
    if handled:
        lines += ["", "| Issue | 留评语 | 标签改 done | 已关闭 | 备注 |", "|---|---|---|---|---|"]
        for item in handled:
            note = item.get("reason", "") if item.get("skipped") else ""
            lines.append(
                f"| #{item.get('issue')} | {'✅' if item.get('commented') else '—'} "
                f"| {'✅' if item.get('labels_updated') else '—'} "
                f"| {'✅' if item.get('closed') else '—'} | {note} |"
            )

    return "\n".join(lines) + "\n"


def _print_result(result: dict[str, Any]) -> None:
    """把结果打印到控制台（Actions 日志里可见）。"""
    print()
    print("=" * 60)
    if result.get("skipped"):
        print(f"⏭️  审核跳过：{result.get('reason', '')}")
        print("=" * 60)
        return

    print(f"提交: {result.get('commit', '')}  {result.get('commit_message', '')}")
    print(f"结论: {'✅ 通过' if result.get('passed') else '❌ 不通过'}  score={result.get('score')}")
    print(f"关联 Issue: {result.get('issues')}")

    stats = result.get("stats") or {}
    if stats:
        print(f"变更: {stats.get('files', 0)} 个文件，"
              f"+{stats.get('additions', 0)} / -{stats.get('deletions', 0)}"
              f"{'（diff 已截断）' if stats.get('truncated') else ''}")

    if result.get("error"):
        print(f"⚠️  审核未完成：{result['error']}")

    for item in result.get("handled") or []:
        if item.get("dry_run"):
            print(f"  · Issue #{item['issue']}（演练模式，未写入 GitHub）")
        elif item.get("skipped"):
            print(f"  · Issue #{item['issue']} 跳过：{item.get('reason', '')}")
        else:
            print(f"  · Issue #{item['issue']} 评论={'✅' if item.get('commented') else '—'} "
                  f"done={'✅' if item.get('labels_updated') else '—'} "
                  f"关闭={'✅' if item.get('closed') else '—'}")
    print("=" * 60)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Agent 代码审核：根据 push 的 diff 调用 LLM 审核，过审则关闭关联 Issue",
    )
    parser.add_argument(
        "--before",
        default=env("BEFORE_SHA"),
        help="push 前的 commit SHA（默认读环境变量 BEFORE_SHA；新分支可为空或全 0）",
    )
    parser.add_argument(
        "--after",
        default=env("COMMIT_SHA") or env("GITHUB_SHA"),
        help="本次 push 后的 commit SHA（默认读 COMMIT_SHA / GITHUB_SHA）",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="仓库全名 owner/repo（默认读 REPO / GITHUB_REPOSITORY / GITHUB_REPO）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("AGENT_DRY_RUN"),
        help="演练模式：只打印将要发布的报告，不写 GitHub（可用 AGENT_DRY_RUN=1 打开）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略“同一提交已审核过”的判断，强制重新评论",
    )
    parser.add_argument(
        "--log-level",
        default=env("AGENT_LOG_LEVEL", "INFO"),
        help="日志级别：DEBUG / INFO / WARNING（默认 INFO）",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """
    程序入口。

    :param argv: 命令行参数（None 表示取 sys.argv）
    :return: 进程退出码
    """
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    after_sha = (args.after or "").strip()
    before_sha = (args.before or "").strip()

    if not after_sha:
        logger.error("缺少本次提交 SHA：请设置环境变量 COMMIT_SHA（Actions 中为 github.sha），"
                     "或使用 --after <sha>")
        _write_step_summary("## ❌ Agent 审核失败\n\n缺少本次提交 SHA（COMMIT_SHA / --after）\n")
        return 2

    logger.info("GitHub Token：%s", mask_secret(github_token()))
    logger.info("仓库：%s", args.repo or github_repo() or "(未配置)")
    logger.info("审核范围：%s ... %s", (before_sha or "(新分支)")[:7], after_sha[:7])

    # ---- 初始化（配置错误要给出明确提示，而不是抛栈） ----
    try:
        reviewer = CodeReviewer(
            repo=args.repo or None,
            dry_run=args.dry_run,
        )
    except (ReviewError, LLMError) as exc:
        message = getattr(exc, "message", str(exc))
        logger.error("初始化失败：%s", message)
        _write_step_summary(f"## ❌ Agent 初始化失败\n\n{message}\n")
        return 1

    # ---- 执行审核 ----
    try:
        result = reviewer.review(before_sha, after_sha, dry_run=args.dry_run, force=args.force)
    except ReviewError as exc:
        logger.error("审核失败：%s", exc.message)
        _write_step_summary(f"## ❌ Agent 审核失败\n\n{exc.message}\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - 兜底，避免 Actions 只看到堆栈
        logger.exception("审核过程发生未预期异常")
        _write_step_summary(f"## ❌ Agent 异常\n\n```\n{exc}\n```\n")
        return 1

    _print_result(result)
    _write_step_summary(_build_summary_markdown(result))

    # LLM 调用失败时返回非 0，让 Actions 变红引起注意（此时只留了“审核未完成”评论，未改 Issue 状态）
    if result.get("error"):
        logger.error("审核未能完成：%s", result["error"])
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
#   t e s t  
 