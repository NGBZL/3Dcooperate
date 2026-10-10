"""
LLM 客户端
----------
封装大模型 Chat Completions 调用，兼容 DeepSeek / OpenAI / 任何 OpenAI 兼容网关。

对外只暴露两个方法：
    * chat(system_prompt, user_prompt)  -> 返回模型输出的文本
    * chat_json(system_prompt, user_prompt) -> 返回解析后的 dict（解析失败自动降级）

设计要点：
1. API 地址 / 模型 / Key 全部可配置，且**空字符串视为未配置**（Actions 里未设置的
   Secret 会注入空串，不能直接用 os.getenv 的默认值参数）
2. 网络失败、5xx、429 自动重试；401/403/404 等明确错误立即抛出中文异常
3. 严格 JSON 解析：自动剥离 ```json 代码块、从散文里提取最外层 JSON 对象
4. 解析失败或字段非法时**降级为“不通过”**，绝不抛异常中断审核流程
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

import requests

from . import config as cfg

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """
    LLM 调用异常（网络 / 鉴权 / 服务端错误）。

    注意：JSON 解析失败**不会**抛这个异常，而是走降级返回，见 chat_json()。
    """

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMClient:
    """
    LLM 客户端。

    :param api_key: API Key；默认取 LLM_API_KEY
    :param api_url: 接口地址；默认取 LLM_API_URL，再默认 DeepSeek 官方地址
    :param model: 模型名；默认取 LLM_MODEL，再默认 deepseek-chat
    :param timeout: 单次请求超时（秒）
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else cfg.env("LLM_API_KEY")).strip()
        self.api_url = (api_url if api_url is not None else cfg.env("LLM_API_URL")).strip() or cfg.DEFAULT_LLM_API_URL
        self.model = (model if model is not None else cfg.env("LLM_MODEL")).strip() or cfg.DEFAULT_LLM_MODEL
        self.timeout = timeout or cfg.LLM_TIMEOUT

        if not self.api_key:
            raise LLMError(
                "LLM_API_KEY 未设置：请在 agent/.env 中填写 LLM_API_KEY，"
                "或在 GitHub 仓库 Settings → Secrets 中配置 LLM_API_KEY"
            )

        # 宽松校验：OpenAI 兼容网关的地址一般以 /chat/completions 结尾
        if not self.api_url.startswith(("http://", "https://")):
            raise LLMError(
                f"LLM_API_URL 格式错误：{self.api_url!r}，应为 https://... 开头的完整地址"
            )

        logger.info(
            "LLM 客户端就绪：model=%s, url=%s, key=%s",
            self.model, self.api_url, cfg.mask_secret(self.api_key),
        )

    # ========================================================
    # 基础调用
    # ========================================================
    def _headers(self) -> dict[str, str]:
        """构造请求头。"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
    ) -> str:
        """
        调用 LLM 并返回文本内容。

        :param system_prompt: 系统提示词
        :param user_prompt: 用户提示词
        :param temperature: 采样温度，默认取配置 LLM_TEMPERATURE
        :return: 模型输出的文本
        :raises LLMError: 鉴权失败、额度不足、网络多次失败等
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": cfg.LLM_TEMPERATURE if temperature is None else temperature,
            "stream": False,
        }

        retries = max(1, cfg.LLM_MAX_RETRIES)
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                response = requests.post(
                    self.api_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("LLM 请求异常（第 %d/%d 次）：%s", attempt, retries, exc)
                if attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                raise LLMError(f"调用 LLM 失败（已重试 {retries} 次）：{exc}") from exc

            # ---- 明确错误：不重试，直接给出可操作的中文提示 ----
            if response.status_code == 401:
                raise LLMError(
                    "LLM_API_KEY 无效或已过期（401）：请检查 agent/.env 或仓库 Secrets 中的 LLM_API_KEY",
                    401,
                )
            if response.status_code == 403:
                raise LLMError("LLM 拒绝访问（403）：Key 可能没有该模型的权限", 403)
            if response.status_code == 404:
                raise LLMError(
                    f"LLM 接口不存在（404）：请检查 LLM_API_URL 是否为 "
                    f"{self.api_url}（需以 /v1/chat/completions 之类结尾）",
                    404,
                )
            if response.status_code == 429:
                logger.warning("LLM 触发限流（429），第 %d/%d 次", attempt, retries)
                if attempt < retries:
                    time.sleep(5 * attempt)
                    continue
                raise LLMError("LLM 触发限流（429），请稍后重试或降低频率", 429)
            if response.status_code >= 500:
                logger.warning("LLM 服务端错误 %s（第 %d/%d 次）",
                               response.status_code, attempt, retries)
                if attempt < retries:
                    time.sleep(3 * attempt)
                    continue
                raise LLMError(f"LLM 服务端持续返回 {response.status_code}，请稍后重试",
                               response.status_code)
            if response.status_code != 200:
                raise LLMError(
                    f"LLM 返回意外状态码 {response.status_code}：{response.text[:200]}",
                    response.status_code,
                )

            # ---- 解析响应体 ----
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMError(f"LLM 返回内容不是合法 JSON：{response.text[:200]}") from exc

            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(
                    f"LLM 响应结构不符合 OpenAI 规范，缺少 choices[0].message.content："
                    f"{json.dumps(data, ensure_ascii=False)[:200]}"
                ) from exc

            if not isinstance(content, str) or not content.strip():
                raise LLMError("LLM 返回了空内容，请检查模型名是否正确")

            return content

        # 逻辑上不会到达这里
        raise LLMError(f"调用 LLM 失败：{last_error}")

    # ========================================================
    # JSON 调用
    # ========================================================
    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        调用 LLM 并把结果解析成审核结论字典。

        **解析失败时不会抛异常**，而是返回降级结果（passed=False，score=0），
        保证“模型乱答 → 默认不通过”而不是让 Action 崩掉。

        :return: 至少包含 passed / score / comment / suggestions 四个键
        """
        try:
            content = self.chat(system_prompt, user_prompt, temperature=temperature)
        except LLMError:
            # API 层面的失败由上层决定如何处理（通常是留个“审核未完成”的评论）
            raise

        parsed = self._extract_json(content)
        if parsed is None:
            logger.error("LLM 输出无法解析为 JSON，降级为不通过。原始输出片段：%s", content[:300])
            return self._fallback(
                f"审核 Agent 无法解析模型输出（不是合法 JSON），已按「不通过」处理。\n\n"
                f"> 原始输出片段：{content[:200]}",
                content,
            )

        normalized = self._normalize(parsed, content)
        if normalized is None:
            logger.error("LLM 输出缺少必要字段，降级为不通过：%s", parsed)
            return self._fallback(
                f"审核 Agent 收到的 JSON 缺少 passed/score 等必要字段，已按「不通过」处理。\n\n"
                f"> 原始输出片段：{content[:200]}",
                content,
            )

        return normalized

    # ========================================================
    # 解析与校验
    # ========================================================
    @staticmethod
    def _extract_json(content: str) -> Optional[dict[str, Any]]:
        """
        从模型输出中提取 JSON 对象。

        依次尝试：
        1. 直接 json.loads
        2. 剥离 ```json ... ``` 代码块后再解析
        3. 扫描首个 “{” 到与之配对的 “}”，取出最外层对象再解析

        :param content: 模型原始输出
        :return: 解析出的字典；失败返回 None
        """
        text = (content or "").strip()
        if not text:
            return None

        # 1) 直接解析
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

        # 2) 去掉 Markdown 代码块围栏
        fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            inner = fence.group(1).strip()
            try:
                data = json.loads(inner)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                text = inner  # 继续走第 3 步的括号扫描

        # 3) 括号配对扫描（应对“前面还有解释文字”的情况）
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]

            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        data = json.loads(candidate)
                        return data if isinstance(data, dict) else None
                    except json.JSONDecodeError:
                        return None

        return None

    @staticmethod
    def _normalize(data: dict[str, Any], raw_content: str) -> Optional[dict[str, Any]]:
        """
        校验并规整 LLM 返回的字段。

        关键规则（修复基线代码的隐患）：
        * **最终是否通过以 score >= PASS_SCORE 为准**，不盲信模型给的 passed 字段；
          两者矛盾时记录告警，并按分数判定。
        * score 支持字符串数字，超出 0~100 会被夹到边界；无法解析则视为解析失败。
        * suggestions 支持字符串或列表，最多保留 8 条。

        :param data: 解析出的原始字典
        :param raw_content: 模型原始输出（用于降级提示）
        :return: 规整后的字典；字段非法时返回 None
        """
        if "score" not in data and "passed" not in data:
            return None

        score: Optional[int] = None
        raw_score = data.get("score")
        if raw_score is not None:
            try:
                # 容忍 "85"、85.0、"85分" 这类写法
                score = int(float(str(raw_score).strip().rstrip("分")))
            except (TypeError, ValueError):
                logger.warning("LLM 返回的 score 无法解析：%r", raw_score)

        if score is None:
            return None

        if score < 0 or score > 100:
            logger.warning("LLM 返回的 score=%s 超出 0~100，已夹到边界", score)
            score = max(0, min(100, score))

        model_passed = data.get("passed")
        if not isinstance(model_passed, bool):
            model_passed = None

        final_passed = score >= cfg.PASS_SCORE
        if model_passed is not None and model_passed != final_passed:
            logger.warning(
                "LLM 的 passed=%s 与 score=%s（通过线 %s）矛盾，以 score 为准",
                model_passed, score, cfg.PASS_SCORE,
            )

        comment = str(data.get("comment") or "").strip()

        suggestions = data.get("suggestions") or []
        if isinstance(suggestions, str):
            suggestions = [suggestions]
        if not isinstance(suggestions, list):
            suggestions = []
        cleaned = [str(item).strip() for item in suggestions if str(item).strip()]

        return {
            "passed": final_passed,
            "score": score,
            "comment": comment,
            "suggestions": cleaned[:8],
            "model_passed": model_passed,
            "parse_failed": False,
            "raw": raw_content[:500],
        }

    @staticmethod
    def _fallback(reason: str, raw_content: str = "") -> dict[str, Any]:
        """
        构造降级结果（默认不通过）。

        :param reason: 写进评语的降级说明
        :param raw_content: 模型原始输出
        :return: 与正常结果同构的字典
        """
        return {
            "passed": False,
            "score": 0,
            "comment": reason,
            "suggestions": [
                "请确认 LLM_MODEL 与 LLM_API_URL 是否匹配（DeepSeek 为 deepseek-chat）",
                "提示词可能被模型误解，可重跑一次或降低 temperature",
            ],
            "model_passed": None,
            "parse_failed": True,
            "raw": (raw_content or "")[:500],
        }
