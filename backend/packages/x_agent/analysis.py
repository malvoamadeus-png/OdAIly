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
# Generic AI/product words such as "token", "launch", "protocol", and
# "contract" are not enough to spend a model request.
PROJECT_CRYPTO_SIGNALS = re.compile(
    r"(?:0x[a-fA-F0-9]{8,}|\b(?:airdrop|tge|fdv|tvl|degen|tokenomics|launchpad|staking|"
    r"mainnet|testnet|onchain|blockchain|web3|crypto|defi|dex|cex|memecoin|meme\s*coin|"
    r"smart\s*contract|liquidity\s*pool|solana|ethereum|arbitrum|optimism|polygon|"
    r"avalanche|aptos|sui|cosmos|elysium|hyperevm|hype(?:-?evm)?|hyperliquid|"
    r"\bton\b|\bbsc\b|bnb\s+chain|base\s+(?:chain|network))\b|"
    r"空投|链上|新币|土狗|发射盘|合约地址|智能合约|代币经济)",
    re.IGNORECASE,
)
PROJECT_TICKER = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{1,14}")
PROJECT_TOKEN_REFERENCE = re.compile(r"(?:\btoken\b|代币)", re.IGNORECASE)
PROJECT_OPERATION_SIGNALS = re.compile(
    r"(?:套利|价差|跨链|资金费率|费率套利|利差|做市|流动性挖矿|挖矿收益|收益率|年化|"
    r"arbitrage|spread|funding\s*rate|cross[- ]chain|bridge|yield|basis\s+trade)",
    re.IGNORECASE,
)
PROJECT_LOGIC_ATTRIBUTION = re.compile(
    r"^(?:(?:该)?作者|账号|发帖者)(?:认为|称|表示|指出|提及|提到|看好|判断|觉得|认为值得关注)[，,:：\s]*",
    re.IGNORECASE,
)
PROJECT_LOGIC_ENGLISH_ATTRIBUTION = re.compile(
    r"^(?:the\s+)?(?:author|account|poster)\s+(?:believes|says|states|notes|mentions|thinks|argues)\s+(?:that\s+)?",
    re.IGNORECASE,
)
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
            or (PROJECT_OPERATION_SIGNALS.search(text) and PROJECT_TICKER.search(text))
        )
    raise ValueError(f"unsupported X Agent module: {module}")


class XAgentAnalyzer:
    """OpenAI-compatible client using the Luna route for analysis and retries."""

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
        self.primary_model = primary_model or os.getenv("X_AGENT_MODEL") or "gpt-6-luna"
        self.fallback_model = fallback_model or os.getenv("X_AGENT_FALLBACK_MODEL") or "gpt-6-luna"
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
                "美股个股、指数、ETF；链上新币、Meme、degen、按头操作不属于这里。纯价格变化、新闻转发、无明确态度的内容返回空 items。"
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
                "你是严格的信息抽取器。对每条帖子分别判断两个维度；两个维度互不替代，可以只命中一个、同时命中或都不命中。"
                "直接推介：只在作者明确看多、建议买入或建议关注某个具体 Crypto 项目/代币，并给出理由时填写。看空、建议卖出、纯项目介绍、教程、持仓披露、新闻转发、抽奖、口号或只提到项目不算直接推介。"
                "操作建议：必须同时具备 (1) 可执行的具体交易/操作路径，(2) 作者明确说出的经济获利来源，例如价差、资金费率、奖励、利息或收益覆盖成本。两项缺一不可。纯教程、单纯买入/跨链入口、没有获利理由的路径、只有事后盈利陈述都不算。"
                "操作建议的利润来源必须直接来自 author_text；不要把教程中的操作步骤推成套利，也不要自行推算价差、费用后的净收益或确定性。作者声称的收益、费用、锁定/赎回限制、风险和时效分别记录；原文没写就填空字符串，不得补造。不得把作者自报收益说成已核实结果。"
                "不要把 BTC、ETH 等主流资产的一般行情讨论当作按头操作，也不要猜测合约、链、平台或官网。只根据 author_text 判断；被引用内容不作为作者观点。"
                "direct_promotion.logic 应简洁说明作者的方向、推荐和理由；operation_advice.action、route 和 profit_source 必须具体且忠于原文。不要使用“作者认为”“作者称”等归因前缀，详情会另行标注为作者主张。"
            )
            shape = {
                "items": [
                    {
                        "project_name": "明确项目名",
                        "ticker": "可空，去掉$",
                        "chain": "可空",
                        "contract_address": "可空，只填原帖明确地址",
                        "official_url": "可空，只填原帖明确官网",
                        "direct_promotion": {
                            "stance": "看多|建议关注|建议买入",
                            "logic": "明确方向、推荐和理由；不命中时为 null",
                        },
                        "operation_advice": {
                            "action": "具体要执行的交易/操作；不命中时为 null",
                            "route": "明确步骤、资产与场所之间的路径",
                            "profit_source": "原文明说的获利来源，缺少时不得命中",
                            "claimed_return": "作者声称的收益/价差原文数据，可空",
                            "fees": "原文明说的费用，可空",
                            "constraints": "锁定、赎回、跨回等限制，可空",
                            "risks": "原文明说的风险，可空",
                            "valid_until": "原文明说的有效时段或过期点，可空",
                        },
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
            direct_raw = raw.get("direct_promotion")
            direct: dict[str, str] | None = None
            if isinstance(direct_raw, dict):
                stance = str(direct_raw.get("stance") or "").strip()
                logic = _project_logic_without_attribution(str(direct_raw.get("logic") or ""))
                if stance in {"看多", "建议关注", "建议买入"} and len(logic) >= 4:
                    direct = {"stance": stance, "logic": logic[:500]}

            operation_raw = raw.get("operation_advice")
            operation: dict[str, str] | None = None
            if isinstance(operation_raw, dict):
                operation = {
                    key: str(operation_raw.get(key) or "").strip()[:500]
                    for key in (
                        "action", "route", "profit_source", "claimed_return", "fees",
                        "constraints", "risks", "valid_until",
                    )
                }
                if not all(len(operation[key]) >= 4 for key in ("action", "route", "profit_source")):
                    operation = None

            if not name or (direct is None and operation is None):
                continue
            url = str(raw.get("official_url") or "").strip()
            if url and not re.match(r"https?://", url, re.IGNORECASE):
                url = ""
            logic = direct["logic"] if direct else f"{operation['action']}；利润来源：{operation['profit_source']}"
            result.append({
                "project_name": name[:160],
                "ticker": str(raw.get("ticker") or "").strip().lstrip("$").upper()[:32],
                "chain": str(raw.get("chain") or "").strip()[:64],
                "contract_address": str(raw.get("contract_address") or "").strip()[:180],
                "official_url": url[:500],
                "logic": logic[:500],
                "direct_promotion": direct,
                "operation_advice": operation,
            })
    return result


def _project_logic_without_attribution(value: str) -> str:
    """Keep a project rationale directly readable in the result table."""
    logic = value.strip()
    logic = PROJECT_LOGIC_ATTRIBUTION.sub("", logic)
    logic = PROJECT_LOGIC_ENGLISH_ATTRIBUTION.sub("", logic)
    return logic.strip()


def _retryable(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 409, 425, 429} or 500 <= error.code < 600
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))
