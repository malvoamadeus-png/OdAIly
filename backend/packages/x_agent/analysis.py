"""Low-cost, structured extraction for X Agent internal views."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


MARKET_SIGNALS = re.compile(
    r"(?:\$[A-Za-z][A-Za-z0-9_]{1,14}|\b(?:btc|bitcoin|eth|ethereum|sol|bnb|xrp|crypto|"
    r"stock|stocks|equity|etf|index|nasdaq|dow|s&p|macro|treasury|fed|rates|long|short|"
    r"bullish|bearish|option|futures|perp)\b|看多|看空|仓位|买入|卖出|美股|纳指|标普|联储|利率)",
    re.IGNORECASE,
)
# Project promotion is Crypto-only. Generic AI/product words such as
# "token", "launch", "protocol", and "contract" are not enough to spend a
# model request.
PROJECT_CRYPTO_SIGNALS = re.compile(
    r"(?:0x[a-fA-F0-9]{8,}|\b(?:airdrop|tge|fdv|tvl|degen|tokenomics|launchpad|staking|"
    r"mainnet|testnet|onchain|blockchain|web3|crypto|defi|dex|cex|memecoin|meme\s*coin|"
    r"smart\s*contract|liquidity\s*pool|solana|ethereum|arbitrum|optimism|polygon|"
    r"avalanche|aptos|sui|cosmos|\bton\b|\bbsc\b|bnb\s+chain|base\s+(?:chain|network))\b|"
    r"空投|链上|新币|土狗|发射盘|合约地址|智能合约|代币经济)",
    re.IGNORECASE,
)
PROJECT_TICKER = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{1,14}")
PROJECT_TOKEN_REFERENCE = re.compile(r"(?:\btoken\b|代币)", re.IGNORECASE)
SENTIMENTS = {"极度狂热", "偏多/乐观", "中性/分歧", "偏空/谨慎", "极度恐慌", "证据不足"}
SCOPES = {"大盘", "Crypto具体标的", "美股具体标的"}


class XAgentModelFailure(RuntimeError):
    """Both configured model routes failed or returned invalid JSON."""


@dataclass(frozen=True)
class AnalysisResult:
    module: str
    actual_model: str
    fallback_reason: str | None
    items: list[dict[str, Any]]


def is_relevant(module: str, text: str) -> bool:
    """Use a cheap recall gate before an API request; final decisions stay AI-based."""
    if module == "market_sentiment":
        return bool(MARKET_SIGNALS.search(text))
    if module == "project_promotion":
        return bool(
            PROJECT_CRYPTO_SIGNALS.search(text)
            or (PROJECT_TICKER.search(text) and PROJECT_TOKEN_REFERENCE.search(text))
        )
    raise ValueError(f"unsupported X Agent module: {module}")


class XAgentAnalyzer:
    """OpenAI-compatible client with Luna primary and Terra fallback."""

    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_attempts: int = 2,
        post_json: Callable[[str, str, bytes, float], dict[str, Any]] | None = None,
    ) -> None:
        self.primary_model = primary_model or os.getenv("X_AGENT_MODEL") or "gpt-5.6-luna"
        self.fallback_model = fallback_model or os.getenv("X_AGENT_FALLBACK_MODEL") or "gpt-5.6-terra"
        self.base_url = (
            base_url
            or os.getenv("X_AGENT_OPENAI_BASE_URL")
            or os.getenv("ODAILY_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ""
        ).rstrip("/")
        self.api_key = api_key or self._default_api_key(self.base_url)
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._post_json = post_json or self._http_post_json
        if not self.base_url or not self.api_key:
            raise RuntimeError("X Agent requires a compatible model base URL and API key")

    @staticmethod
    def _default_api_key(base_url: str) -> str:
        """Use the local LiteLLM credential when the configured route is loopback."""
        explicit = os.getenv("X_AGENT_OPENAI_API_KEY") or ""
        if explicit:
            return explicit
        if base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://127.0.0.1:", "https://localhost:")):
            return os.getenv("LITELLM_MASTER_KEY") or os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        return os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""

    def analyze(self, module: str, source: dict[str, Any]) -> AnalysisResult:
        prompt = self._prompt(module, source)
        primary_error: str | None = None
        try:
            return AnalysisResult(module, self.primary_model, None, self._request(self.primary_model, prompt, module))
        except Exception as exc:
            primary_error = f"{type(exc).__name__}: {exc}"
        if not self.fallback_model or self.fallback_model == self.primary_model:
            raise XAgentModelFailure(primary_error or "primary model failed")
        try:
            items = self._request(self.fallback_model, prompt, module)
            return AnalysisResult(module, self.fallback_model, primary_error, items)
        except Exception as exc:
            raise XAgentModelFailure(f"primary={primary_error}; fallback={type(exc).__name__}: {exc}") from exc

    def _request(self, model: str, prompt: str, module: str) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "reasoning_effort": "none",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                raw = self._post_json(self.base_url, self.api_key, payload, self.timeout)
                content = _message_content(raw)
                parsed = _parse_json_object(content)
                return _validate_items(module, parsed)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts and _retryable(exc):
                    time.sleep(min(4.0, 0.75 * (2**attempt)))
                    continue
                break
        assert last_error is not None
        raise last_error

    @staticmethod
    def _http_post_json(base_url: str, api_key: str, body: bytes, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("model endpoint returned non-object JSON")
        return payload

    @staticmethod
    def _prompt(module: str, source: dict[str, Any]) -> str:
        own_text = str(source.get("text") or "")
        fallback_text = "" if source.get("activity_type") == "quote" else str(source.get("expanded_text") or "")
        text = " ".join((own_text or fallback_text).split())[:3500]
        evidence = {
            "tweet_id": str(source.get("tweet_id") or ""),
            "account": str(source.get("account_screen_name") or ""),
            "created_at": str(source.get("created_at_iso") or ""),
            "activity_type": str(source.get("activity_type") or "original"),
            "author_text": text,
        }
        if module == "market_sentiment":
            rules = (
                "你是严格的信息抽取器。只抽取作者明确表达态度的主流二级市场观点。允许大盘、主流中心化交易所交易的 Crypto、"
                "美股个股、指数、ETF；链上新币、Meme、degen、项目推介不属于这里。纯价格变化、新闻转发、无明确态度的内容返回空 items。"
                "不要把涨跌自动理解为狂热或恐慌。只可根据 author_text 判断被跟踪账号的态度。"
            )
            shape = {
                "items": [
                    {
                        "scope": "大盘|Crypto具体标的|美股具体标的",
                        "instrument_name": "标准名或清晰范围",
                        "ticker": "可空，去掉$",
                        "sentiment": "极度狂热|偏多/乐观|中性/分歧|偏空/谨慎|极度恐慌|证据不足",
                        "reason": "不超过80字，必须来自原帖",
                    }
                ]
            }
        elif module == "project_promotion":
            rules = (
                "你是严格的信息抽取器。只抽取链上新项目、degen 或低流动性项目中，作者给出可理解逻辑、理由或看法的内容。"
                "允许项目研究、推荐、持仓披露和项目介绍；纯新闻、转发、抽奖、口号、没有项目主体或没有逻辑时返回空 items。"
                "不要把 BTC、ETH 等主流资产的一般行情讨论当作项目推介，也不要猜测合约、链或官网。只可根据 author_text "
                "判断被跟踪账号的项目观点。"
            )
            shape = {
                "items": [
                    {
                        "project_name": "明确项目名",
                        "ticker": "可空，去掉$",
                        "chain": "可空",
                        "contract_address": "可空，只填原帖明确地址",
                        "official_url": "可空，只填原帖明确官网",
                        "logic": "不超过120字，作者为何提及/推介",
                    }
                ]
            }
        else:
            raise ValueError(f"unsupported X Agent module: {module}")
        return (
            f"{rules}\n"
            "只返回合法 JSON，不要 markdown，不要补充说明。JSON 格式："
            f"{json.dumps(shape, ensure_ascii=False)}\n"
            f"原帖：{json.dumps(evidence, ensure_ascii=False)}"
        )


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("model response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    raise ValueError("model response has no text content")


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("model response does not contain JSON")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("model response JSON is not an object")
    return value


def _validate_items(module: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("model response items must be a list")
    result: list[dict[str, Any]] = []
    for raw in raw_items[:8]:
        if not isinstance(raw, dict):
            continue
        if module == "market_sentiment":
            scope = str(raw.get("scope") or "").strip()
            name = str(raw.get("instrument_name") or "").strip()
            ticker = str(raw.get("ticker") or "").strip().lstrip("$").upper()
            sentiment = str(raw.get("sentiment") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            if scope not in SCOPES or sentiment not in SENTIMENTS or not (name or ticker) or not reason:
                continue
            result.append({"scope": scope, "instrument_name": name or ticker, "ticker": ticker, "sentiment": sentiment, "reason": reason[:300]})
        elif module == "project_promotion":
            name = str(raw.get("project_name") or "").strip()
            logic = str(raw.get("logic") or "").strip()
            if not name or len(logic) < 4:
                continue
            url = str(raw.get("official_url") or "").strip()
            if url and not re.match(r"https?://", url, re.IGNORECASE):
                url = ""
            result.append({
                "project_name": name[:160],
                "ticker": str(raw.get("ticker") or "").strip().lstrip("$").upper()[:32],
                "chain": str(raw.get("chain") or "").strip()[:64],
                "contract_address": str(raw.get("contract_address") or "").strip()[:180],
                "official_url": url[:500],
                "logic": logic[:500],
            })
    return result


def _retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 409, 425, 429} or 500 <= error.code < 600
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
