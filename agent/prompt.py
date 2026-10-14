"""
审核提示词模板
--------------
包含三部分：

* SYSTEM_PROMPT         —— 系统提示词，规定角色与 6 个审核维度
* USER_PROMPT_TEMPLATE  —— 用户提示词，注入提交信息、关联 Issue、代码 diff
* REPORT_TEMPLATE       —— 写回 GitHub Issue 的 Markdown 报告模板

注意：这两个提示词模板都会走 str.format()，所以正文里出现的半角花括号
（例如 JSON 示例）必须写成 {{ }} 转义。
"""

from __future__ import annotations

from . import config as cfg

# ============================================================
# 系统提示词
# ============================================================
SYSTEM_PROMPT = """你是一个严格的代码审核员，负责审核学生三维重建项目的代码提交。

你必须从以下 6 个维度审核：
1. 语法正确性：代码能否正常编译 / 运行，有没有明显的语法错误
2. 逻辑正确性：有没有 bug、边界条件遗漏、逻辑漏洞
3. 安全性：有没有注入、密钥泄露、越权、危险命令等风险
4. 测试覆盖：有没有对应的测试用例
5. 代码风格：命名、注释、结构是否清晰一致
6. 提交信息：是否清晰描述了本次变更

评分规则（0~100 整数）：
- 90-100：优秀，可以直接合并
- 70-89：良好，有小问题但不影响合并
- 50-69：及格，需要修改后再提交
- 0-49：不及格，存在严重问题

通过标准：score >= 70 才算通过（passed = true）。

输出要求（非常重要）：
- 只输出一个 JSON 对象，不要输出任何解释性文字
- 不要用 Markdown 代码块包裹
- JSON 必须严格合法（双引号、无尾随逗号）"""


# ============================================================
# 用户提示词
# ============================================================
USER_PROMPT_TEMPLATE = """请审核以下代码提交。

## 提交信息
{commit_message}

## 关联的 Issue
{issue_info}

## 代码变更（diff）
{diff}

## 输出格式（严格 JSON，不要有任何其他内容）
{{
    "passed": true 或 false,
    "score": 0 到 100 的整数,
    "comment": "详细评语（中文，300 字以内，分点说明）",
    "suggestions": ["改进建议1", "改进建议2"]
}}

## 提醒
- passed 必须与 score 一致：score >= {pass_score} 时为 true，否则为 false
- comment 与 suggestions 都用中文，具体指出文件名与问题所在
- 如果 diff 被截断（出现“已截断”字样），请只针对可见部分评审，并在评语中说明"""


# ============================================================
# Issue 报告模板（Markdown，写进 Issue 评论）
# ============================================================
REPORT_TEMPLATE = """## 🤖 Agent 代码审核报告

| 项目 | 内容 |
|------|------|
| 审核结果 | {result_badge} |
| 综合评分 | `{score} / 100`（通过线 {pass_score}） |
| 变更规模 | {diff_stat} |
| 关联提交 | `{short_sha}` {commit_title} |

### 📝 评语

{comment}
{suggestions_section}{action_footer}
### 🔍 审核维度

语法正确性 · 逻辑正确性 · 安全性 · 测试覆盖 · 代码风格 · 提交信息

---
<sub>🤖 由 Agent 自动生成 · 模型：{model} · 时间：{timestamp}</sub>
{marker}
"""

# 通过 / 不通过时不同的行为说明，让提交者一眼看懂下一步
PASSED_FOOTER = "\n> ✅ 已自动将 Issue 标记为 `done` 并关闭。\n"
FAILED_FOOTER = "\n> ⏸️ Issue 状态保持不变，请按上面的建议修改后再次提交（提交信息里带上同一个 Issue 编号）。\n"
# PR 场景（只审核不定案）：审核通过但先不标记完成，等 PR 合并到主干
PASSED_PR_FOOTER = ("\n> ✅ 本次改动审核通过。这是 Pull Request 的提交，所以**先不标记完成**："
                    "等这个 PR 合并到主干后，会自动把 Issue 标记为 `done` 并关闭。\n")


def build_user_prompt(commit_message: str, issue_info: str, diff: str) -> str:
    """
    渲染用户提示词。

    :param commit_message: 提交信息
    :param issue_info: 关联 Issue 的信息（可能为空串）
    :param diff: 代码变更 diff（可能已被截断）
    :return: 完整的用户提示词
    """
    return USER_PROMPT_TEMPLATE.format(
        commit_message=commit_message or "(无提交信息)",
        issue_info=issue_info or "(未关联到 Issue)",
        diff=diff or "(无代码变更)",
        pass_score=cfg.PASS_SCORE,
    )
