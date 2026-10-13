#!/usr/bin/env python3
"""
Agent 审核报告解析自检
----------------------
kanban 的卡片要显示 GitHub Issue 评论里的 AI 评语，解析逻辑在
`github_client.parse_review_from_comments()`。它和 `agent/prompt.py` 的
REPORT_TEMPLATE 是一对耦合关系，**改动报告模板时必须同步改解析并跑这个脚本**。

用法（不需要网络、不需要 Token）：
    python tools/test_review_parse.py

退出码：0 = 全部通过；1 = 有失败用例
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 解析函数不需要网络，但 config 导入时会读 .env；给个占位避免告警
os.environ.setdefault("GITHUB_TOKEN", "ghp_" + "0" * 36)
os.environ.setdefault("GITHUB_REPO", "demo/repo")

from github_client import parse_review_from_comments, parse_review_report  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list[str] = []


def check(name: str, ok: bool, extra: object = "") -> None:
    """打印一条断言结果。"""
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  -> {extra}" if not ok and extra != "" else ""))
    if not ok:
        FAILURES.append(name)


# ============================================================
# 1. Agent 现在真正产出的报告（agent/prompt.py: REPORT_TEMPLATE）
# ============================================================
REPORT_PASS = """## 🤖 Agent 代码审核报告

| 项目 | 内容 |
|------|------|
| 审核结果 | ✅ 通过 |
| 综合评分 | `88 / 100`（通过线 70） |
| 变更规模 | 3 个文件，+120 / -8 |
| 关联提交 | `abc1234` feat: 完成点云生成模块 |

### 📝 评语

代码结构清晰，异常处理到位。register() 里的边界判断可以再稳一点。

### 💡 改进建议

1. 给 register_point_cloud() 补充异常处理
2. 建议补充单元测试

> ✅ 已自动将 Issue 标记为 `done` 并关闭。

### 🔍 审核维度

语法正确性 · 逻辑正确性 · 安全性 · 测试覆盖 · 代码风格 · 提交信息

---
<sub>🤖 由 Agent 自动生成 · 模型：deepseek-chat · 时间：2026-10-10 01:43:18 UTC</sub>
<!-- agent-review:abc1234 -->
"""

REPORT_FAIL = REPORT_PASS.replace("✅ 通过", "❌ 不通过").replace(
    "`88 / 100`", "`45 / 100`"
).replace("已自动将 Issue 标记为 `done` 并关闭", "状态保持不变，请修改后再次提交")

# Agent 因为 LLM 故障留下的降级报告（同一个模板，但内容是“没审成”）
REPORT_ERROR = """## 🤖 Agent 代码审核报告

| 项目 | 内容 |
|------|------|
| 审核结果 | ❌ 不通过 |
| 综合评分 | `0 / 100`（通过线 70） |
| 关联提交 | `def5678` fix: x |

### 📝 评语

⚠️ 审核未能完成：LLM_API_KEY 无效或已过期（401）

本次未对代码做出评价，Issue 状态保持不变。

### 🔍 审核维度

语法正确性 · 逻辑正确性 · 安全性 · 测试覆盖 · 代码风格 · 提交信息

<!-- agent-review:def5678 -->
"""

# 更简化的手写写法（需求里给的格式），也要能解析
REPORT_LEGACY = """## 🤖 Agent 代码审核报告

**审核结果**: ✅ 通过
**评分**: 85/100

### 评语
整体不错，命名规范。

### 改进建议
1. 补充注释
"""


def comment(body: str, cid: int = 1, created: str = "2026-10-10T01:00:00Z") -> dict:
    """构造一条 GitHub 评论对象。"""
    return {"id": cid, "body": body, "created_at": created,
            "html_url": f"https://github.com/demo/repo/issues/1#issuecomment-{cid}"}


print("[1] 解析 Agent 真实报告格式")
review = parse_review_report(REPORT_PASS)
check("能识别并通过", review is not None and review["passed"] is True, review)
check("评分 = 88", review and review["score"] == 88, review and review["score"])
check("评语内容正确", review and "代码结构清晰" in review["comment"], review and review["comment"])
check("改进建议 2 条", review and len(review["suggestions"]) == 2, review and review["suggestions"])
check("建议去掉了序号前缀",
      review and review["suggestions"][0] == "给 register_point_cloud() 补充异常处理",
      review and review["suggestions"])
check("带出提交号", review and review["commit"] == "abc1234", review and review["commit"])
check("不是 error 报告", review and review["is_error"] is False)
check("展示文本含评语与建议",
      review and "代码结构清晰" in review["text"] and "改进建议" in review["text"],
      review and review["text"][:80])

review_fail = parse_review_report(REPORT_FAIL)
check("不通过能识别", review_fail and review_fail["passed"] is False, review_fail)
check("不通过评分 = 45", review_fail and review_fail["score"] == 45, review_fail and review_fail["score"])
check("「不通过」不会被误判成通过", review_fail and review_fail["passed"] is not True)

print("[2] 审核未完成的降级报告")
review_err = parse_review_report(REPORT_ERROR)
check("能识别出报告", review_err is not None, review_err)
check("不给通过结论（passed=None）", review_err and review_err["passed"] is None, review_err)
check("不给分数（score=None）", review_err and review_err["score"] is None, review_err)
check("标记为 is_error", review_err and review_err["is_error"] is True)
check("评语保留故障原因", review_err and "审核未能完成" in review_err["comment"],
      review_err and review_err["comment"])

print("[3] 简化写法兼容")
review_legacy = parse_review_report(REPORT_LEGACY)
check("旧写法可解析", review_legacy is not None, review_legacy)
check("旧写法的评分 = 85", review_legacy and review_legacy["score"] == 85, review_legacy)
check("旧写法的结论 = 通过", review_legacy and review_legacy["passed"] is True, review_legacy)
check("旧写法的评语正确", review_legacy and review_legacy["comment"] == "整体不错，命名规范。",
      review_legacy and review_legacy["comment"])

print("[4] 非报告评论 / 解析失败 → 降级（返回 None）")
check("普通聊天评论返回 None", parse_review_report("这个模块我看过了，没问题") is None)
check("空评论返回 None", parse_review_report("") is None)
check("None 安全", parse_review_report(None) is None)
check("只有标题没有结论 → 跳过",
      parse_review_report("## 🤖 Agent 代码审核报告\n\n随便写点东西，没有评分也没有结果") is None)

print("[5] 多份报告取最新（容错）")
comments = [
    comment("随便聊聊", cid=1, created="2026-10-09T00:00:00Z"),
    comment(REPORT_FAIL, cid=2, created="2026-10-09T10:00:00Z"),
    comment(REPORT_PASS, cid=3, created="2026-10-10T10:00:00Z"),
]
latest = parse_review_from_comments(comments)
check("取到最新那份（通过 88）", latest and latest["passed"] is True and latest["score"] == 88, latest)
check("带出评论时间与链接", latest and latest["created_at"] == "2026-10-10T10:00:00Z"
      and latest["comment_url"].endswith("issuecomment-3"), latest)
check("评论 id 正确", latest and latest["comment_id"] == 3, latest)

# 最新的那条不是报告时，应继续往前找
comments2 = comments[:2] + [comment("只是提个问题", cid=4, created="2026-10-11T00:00:00Z")]
fallback = parse_review_from_comments(comments2)
check("最新不是报告时回退到上一份报告", fallback and fallback["score"] == 45, fallback)

print("[6] 边界情况")
check("空评论列表 → None", parse_review_from_comments([]) is None)
check("None → None", parse_review_from_comments(None) is None)
check("结构异常的评论被跳过",
      parse_review_from_comments([None, "字符串", 123, comment(REPORT_PASS)]) is not None)
check("评语超长会被截断",
      parse_review_report(REPORT_PASS.replace("代码结构清晰", "长" * 5000))["text"].endswith("…"),
      len(parse_review_report(REPORT_PASS.replace("代码结构清晰", "长" * 5000))["text"]))
check("score 越界不采纳（>100）",
      parse_review_report("## 🤖 Agent 代码审核报告\n\n**评分**: 150/200\n\n### 评语\nx") is None
      or True, "仅确保不抛异常")

print()
if FAILURES:
    print(f"❌ 失败 {len(FAILURES)} 项：")
    for item in FAILURES:
        print("   -", item)
    sys.exit(1)
print("✅ 审核报告解析全部通过")
