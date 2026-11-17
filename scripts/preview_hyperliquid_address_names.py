#!/usr/bin/env python3
"""Generate offline Hyperliquid address-name suggestions from explicit JSON snapshots."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from packages.x_processing.ai_client import OpenAIResponsesClient


MODEL = "gpt-6-luna"
PROMPT_VERSION = "highlight-name-v6"
REQUIRED_FIELDS = ("sample_id", "address_prefix", "as_of", "coverage", "assets", "account_pnl", "events", "current_positions")
ALLOWED_TOP_LEVEL = {"feature_name", "evidence_ids", "reason", "alternatives"}
JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "feature_name": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "alternatives": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "feature_name": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["feature_name", "evidence_ids", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["feature_name", "evidence_ids", "reason", "alternatives"],
    "additionalProperties": False,
}


def load_samples(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    samples = value.get("samples") if isinstance(value, dict) else value
    if not isinstance(samples, list):
        raise ValueError("input must be a JSON array or an object with a samples array")
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ValueError(f"samples[{index}] must be an object")
        missing = [field for field in REQUIRED_FIELDS if field not in sample]
        if missing:
            raise ValueError(f"samples[{index}] missing fields: {', '.join(missing)}")
        if not isinstance(sample["sample_id"], (str, int)) or not str(sample["sample_id"]).strip():
            raise ValueError(f"samples[{index}].sample_id must be a non-empty string or integer")
        if not isinstance(sample["address_prefix"], str) or not re.fullmatch(r"0x[0-9a-fA-F]{2,}", sample["address_prefix"].strip()):
            raise ValueError(f"samples[{index}].address_prefix must be a short 0x hexadecimal prefix")
        for field in ("assets", "events", "current_positions"):
            if not isinstance(sample[field], list):
                raise ValueError(f"samples[{index}].{field} must be an array")
        if not isinstance(sample["account_pnl"], dict) or not isinstance(sample["coverage"], (dict, list, str)):
            raise ValueError(f"samples[{index}].account_pnl must be an object and coverage an object, array, or string")
        if not isinstance(sample["as_of"], str) or not sample["as_of"].strip():
            raise ValueError(f"samples[{index}].as_of must be a non-empty string")
    sample_ids = [str(sample["sample_id"]) for sample in samples]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_id values must be unique")
    return samples


def evidence_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"evidence_id", "event_id", "id"} and isinstance(child, (str, int)):
                found.add(str(child))
            elif key == "evidence_ids" and isinstance(child, list):
                found.update(str(item) for item in child if isinstance(item, (str, int)))
            found.update(evidence_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(evidence_ids(child))
    return found


def load_llm_env(path: Path) -> None:
    allowed_prefixes = ("HL_NAMING_", "ODAILY_LLM_", "OPENAI_", "X_AGENT_OPENAI_")
    allowed_names = {"LITELLM_MASTER_KEY"}
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0]
        key = key.removeprefix("export ").strip()
        if key not in allowed_names and not key.startswith(allowed_prefixes):
            continue
        value = stripped.split("=", 1)[1]
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)


def model_settings() -> tuple[str, str, str]:
    api_key = (os.getenv("HL_NAMING_OPENAI_API_KEY") or os.getenv("X_AGENT_OPENAI_API_KEY")
               or os.getenv("LITELLM_MASTER_KEY") or os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    base_url = (os.getenv("HL_NAMING_OPENAI_BASE_URL") or os.getenv("X_AGENT_OPENAI_BASE_URL")
                or os.getenv("ODAILY_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1")
    api_style = (os.getenv("HL_NAMING_OPENAI_API_STYLE") or os.getenv("X_AGENT_OPENAI_API_STYLE")
                 or os.getenv("ODAILY_LLM_API_STYLE") or "chat_completions")
    if api_style not in {"responses", "chat_completions"}:
        raise ValueError("LLM API style must be responses or chat_completions")
    return api_key, base_url, api_style


def prompt_for(sample: dict[str, Any], allowed_evidence: set[str]) -> str:
    directions = (
        "你为 Hyperliquid 交易地址拟一个醒目的中文特征名。只根据给出的脱敏画像命名，不推断身份。"
        "优先突出一个单一鲜明的亮点或特长，不求全面；不必平衡其他资产或交易的负面表现，也不要使用‘偏科’。"
        "有突出持仓规模等中性特点时也优先于负面称谓；只有缺少正面或中性亮点时才考虑负面特点。"
        "命名是编辑给地址起一个自然易记的称号。将轮数、胜场、持仓单位等数据留在理由中，不把它们逐字堆成名字。"
        "可自由采用‘资产+特点+巨鲸/交易员’或‘资产+大多头/大空头’等简洁中文结构，不必固定模板；"
        "避免‘十五轮十三胜’、‘百万单位多头’等数据摘要名，以及‘兑现者’、‘减仓者’等生硬称呼。"
        "称谓要短，通常只需资产名加一个亮点，再加巨鲸或交易员。不要在名字里放金额、数量、胜场等随时变化的数字，资产符号本身的数字除外。"
        "使用自然表达：‘爆赚’优于‘高额平仓获利’，‘大多头’优于‘近九千万美元多头’。"
        "硬性命名格式：只要根据短期胜率或连胜起名，主名和备选名都必须以‘近期’开头。"
        "资产称谓使用中文读者熟悉的名字：xyz:BRENTOIL/BRENTOIL/xyz:CL/CL在称谓里简写‘原油’，xyz:GOLD写‘黄金’，xyz:SILVER写‘白银’；加密资产保留SOL、ZEC等符号。称谓无需分清布伦特和WTI等细分类别，精确合约标识保留在内部证据。"
        "xyz:SP500称‘标普500’，xyz:NVDA称‘英伟达’，xyz:SNDK称‘闪迪’；这些只是展示名，不能合并不同合约统计。"
        "unrealized_pnl仅是浮盈亏，不是已经赚到的利润；仅有浮盈可用‘浮盈’或持仓规模命名，不得写已兑现爆赚。"
        "当前持仓大可以称大多头，但final_position为null的已清仓资产，主名和备选都不能只称大空头或大多头；可以用爆赚等已经发生的历史结果。"
        "reason只说明自身证据和适用范围，不比较其他资产的样本多少，避免把轮数、成交数量和金额混为一谈。"
        "先比较各项亮点的盈利幅度、胜率、样本数及新闻相关性，再选最突出的一项，不随意挑较弱亮点。"
        "爆赚要求绝对盈利规模也突出，不能只因某资产在一个很小的交易样本中盈利最多就用强称谓。"
        "若历史资产盈利规模较小，但事件时持仓规模显著更突出，优先选择有辨识度的持仓亮点；不是强制选择本次资产。"
        "备选与主名适用同一事实和时效要求；过期账户PnL不能单独支撑备选称谓，不能用解释文字抵消名称事实缺陷。"
        "若样本有多个资产，主名可突出该地址最强的资产特长，不必与本次动作资产相同；理由须说明为何选它。"
        "允许使用‘爆赚’、‘超高胜率’、‘顶级交易员’等强判断，但每项必须有直接匹配的证据和量化口径。"
        "观察窗口不等于全历史；盈利不等于胜率；历史动作不得写成当前敞口。不要声称市场排名或使用‘最大/第一’等全市场比较。"
        "逐项遵守 account_pnl 中的 freshness、usage 和 eligible_for_current_total_pnl_copy 字段；若累计 PnL 不符合当前引用资格，只能作历史背景，"
        "不得据此写当前总盈利标题或主名称。标题样例模拟在 as_of 时点写新闻，只使用当时已有事实；查询时当前仓位不能据此推断。"
        "正文以外由运行报告统一标明历史时点，标题无需写‘截至某日的历史回放’‘观察窗口’等内部过程词。优先从资产、事件和当前持仓中选有力且合格的证据。"
        "feature_name 只能写特征名，不包含地址或地址前缀；程序会在名称后直接拼接真实前缀。名称不得出现‘最大’、‘第一’或‘偏科’。"
        "证据仅能引用 allowed_evidence_ids 中的 ID。reason 简短说明称谓和证据的对应关系及适用窗口。"
        "给出1-3个有证据支持的备选名称，主名与备选不要重复。reason只说明选择原因和适用范围，不复写金额、胜场、百分比等数字，不自行计算。标题由程序从真实事件净动作组装。"
        "只能输出指定 JSON 对象。"
    )
    return directions + "\n\n" + json.dumps(
        {"allowed_evidence_ids": sorted(allowed_evidence), "sample": sample,
         "output_schema": {"feature_name": "string", "evidence_ids": ["string"], "reason": "string",
                           "alternatives": [{"feature_name": "string", "evidence_ids": ["string"], "reason": "string"}]}},
        ensure_ascii=False,
    )


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("model response did not contain a JSON object")
        result = json.loads(match.group(0))
    if not isinstance(result, dict):
        raise ValueError("model response must be a JSON object")
    return result


def validate_result(result: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    if set(result) - ALLOWED_TOP_LEVEL:
        raise ValueError("model response contains unsupported fields")
    if not all(isinstance(result.get(key), str) and result[key].strip() for key in ("feature_name", "reason")):
        raise ValueError("model response needs non-empty feature_name and reason")
    allowed = evidence_ids(sample)

    def validate_citation(feature: dict[str, Any]) -> None:
        citations = feature.get("evidence_ids")
        if not isinstance(citations, list) or not all(isinstance(item, str) for item in citations):
            raise ValueError("each name must include an evidence_ids string array")
        if not citations or set(citations) - allowed:
            raise ValueError("name cites missing or unknown evidence IDs")

    validate_citation(result)
    alternatives = result.get("alternatives")
    if not isinstance(alternatives, list) or len(alternatives) > 3:
        raise ValueError("alternatives must be an array of at most three names")
    for alternative in alternatives:
        if not isinstance(alternative, dict) or not all(
            isinstance(alternative.get(key), str) and alternative[key].strip()
            for key in ("feature_name", "reason")
        ):
            raise ValueError("each alternative needs a feature_name and reason")
        validate_citation(alternative)
    prefix = sample["address_prefix"].strip()
    address_pattern = re.compile(r"0x[0-9a-fA-F]{4,}|[1-9A-HJ-NP-Za-km-z]{12,}")
    for name in [result["feature_name"], *(item["feature_name"] for item in alternatives)]:
        if address_pattern.search(name) or prefix in name or re.search(r"最大|第一|偏科", name):
            raise ValueError("feature_name must not contain an address or its prefix")
    return result


def money(value: Any) -> str:
    amount = abs(Decimal(str(value)))
    if amount >= Decimal("100000000"):
        number = format((amount / Decimal("100000000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")
        return f"约{number}亿美元"
    number = (amount / Decimal("10000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if number == 0:
        return f"约{amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}美元"
    return f"约{number}万美元"


def event_titles(sample: dict[str, Any], name: str) -> list[str]:
    events = sorted(sample["events"], key=lambda event: str(event.get("observed_at", "")), reverse=True)
    if not events:
        return []
    event = events[0]
    coin = str(event.get("coin", ""))
    display = {"xyz:BRENTOIL": "布伦特原油", "xyz:GOLD": "黄金", "xyz:SILVER": "白银",
               "xyz:SP500": "标普500", "xyz:NVDA": "英伟达", "xyz:SNDK": "闪迪"}.get(coin, coin.rsplit(":", 1)[-1])
    kinds = set(event.get("kinds", []))
    position = event.get("final_position")
    if position and kinds.intersection({"profit_giveback", "large_profit", "large_loss"}):
        size = Decimal(str(position["size"]))
        pnl = Decimal(str(position["unrealized_pnl"]))
        side = "多单" if size > 0 else "空单"
        if "profit_giveback" in kinds and pnl > 0:
            return [f"{name}所持{display}{side}浮盈回吐至{money(pnl)}"]
        label = "浮盈" if pnl >= 0 else "浮亏"
        return [f"{name}所持{display}{side}{label}扩大至{money(pnl)}"]
    net = event.get("net_position_change") or {}
    if not net:
        return []
    side = net.get("side") or net.get("previous_side") or "仓位"
    if net.get("closed"):
        title = f"{name}清仓{display}{side}"
        realized = Decimal(str((event.get("actions") or {}).get("ordinary_closed_pnl", "0")))
        if realized:
            title += f"，实现{money(realized)}{'盈利' if realized > 0 else '亏损'}"
    else:
        direction = str(net.get("direction", ""))
        if direction in {"加仓", "增仓", "增加", "增持"}:
            verb = "净增持"
        elif direction in {"减仓", "减少", "减持"}:
            verb = "净减持"
        else:
            return []
        title = f"{name}{verb}{money(net.get('estimated_value', '0'))}{display}{side}"
        realized = Decimal(str((event.get("actions") or {}).get("ordinary_closed_pnl", "0")))
        if "large_realized_result" in kinds and realized:
            title += f"，实现{money(realized)}{'盈利' if realized > 0 else '亏损'}"
    return [title]


def enrich(sample: dict[str, Any], result: dict[str, Any], model: str) -> dict[str, Any]:
    prefix = sample["address_prefix"].strip()
    name = f"{result['feature_name']}{prefix}"
    return {
        "sample_id": sample["sample_id"],
        "address_prefix": prefix,
        "as_of": sample["as_of"],
        "status": "success",
        "requested_model": model,
        "response_model": None,
        "name": name,
        **result,
        "title_examples": event_titles(sample, name),
        "alternatives": [
            {**item, "name": f"{item['feature_name']}{prefix}"} for item in result["alternatives"]
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env", help="Local env file; only approved LLM keys are loaded.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview records without loading model settings or making requests.")
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--retries", type=int, default=1, help="Additional retries per sample, always using gpt-6-luna.")
    args = parser.parse_args()
    started_at = datetime.now(UTC).isoformat()
    started = time.monotonic()
    samples = load_samples(args.input)
    records: list[dict[str, Any]] = []
    model = MODEL
    if args.dry_run:
        records = [
            {"sample_id": sample["sample_id"], "address_prefix": sample["address_prefix"],
             "as_of": sample["as_of"], "status": "validated", "available_evidence_ids": sorted(evidence_ids(sample))}
            for sample in samples
        ]
    else:
        load_llm_env(args.env_file)
        api_key, base_url, api_style = model_settings()
        if not api_key:
            raise SystemExit("AI 配置缺少 API key；请检查现有本地 LLM 环境变量。")
        client = OpenAIResponsesClient(
            api_key=api_key,
            base_url=base_url,
            api_style=api_style,
            timeout_seconds=max(1.0, args.timeout),
            max_attempts=max(1, min(args.retries + 1, 5)),
            backoff_seconds=1.0,
        )
        for sample in samples:
            try:
                generated = client.generate_text(
                    model=MODEL,
                    prompt=prompt_for(sample, evidence_ids(sample)),
                    text_format={"type": "json_schema", "name": "hyperliquid_address_name", "strict": True, "schema": JSON_SCHEMA},
                    reasoning_effort="low",
                )
                validated = validate_result(parse_json_object(generated), sample)
                records.append(enrich(sample, validated, MODEL))
            except Exception as exc:
                records.append({
                    "sample_id": sample["sample_id"], "address_prefix": sample["address_prefix"],
                    "as_of": sample["as_of"], "status": "failed", "requested_model": MODEL,
                    "response_model": None, "error": f"{type(exc).__name__}: {exc}",
                })
    payload = {
        "mode": "dry_run" if args.dry_run else "model",
        "prompt_version": PROMPT_VERSION,
        "requested_model": MODEL,
        "response_model": None,
        "started_at": started_at,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "results": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {status: sum(item["status"] == status for item in records) for status in {item["status"] for item in records}}
    print(json.dumps({"output": str(args.output), "requested_model": MODEL,
                      "response_model": None,
                      "runtime_seconds": payload["runtime_seconds"], "counts": counts}, ensure_ascii=False))
    return int(any(item["status"] == "failed" for item in records))


if __name__ == "__main__":
    raise SystemExit(main())
