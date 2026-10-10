"""
Agent 包
-------
代码审核 Agent：根据 push 的 diff 调用 LLM 审核，过审则把 Issue 标记 done 并关闭。

对外主要暴露两个类：
    * CodeReviewer —— 审核主流程（GitHub API + LLM）
    * LLMClient    —— 大模型调用封装（兼容 DeepSeek / OpenAI）
"""

from .llm_client import LLMClient, LLMError
from .reviewer import CodeReviewer, ReviewError

__all__ = ["CodeReviewer", "ReviewError", "LLMClient", "LLMError"]

__version__ = "1.0.0"
