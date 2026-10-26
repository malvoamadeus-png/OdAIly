"""Fast Meme narrative: local evidence in, OdAIly Luna prose out."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

from . import narrative_v2


INTERFACE_VERSION = "2026-09-01"
FAST_SOURCES = frozenset({"telegram", "x", "fomo_thesis"})


class FastEvidenceProvider(Protocol):
    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class InMemoryFastEvidenceAdapter:
    bundle: dict[str, Any]

    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]:
        del chain, contract, symbol, request_id
        return normalize_bundle(self.bundle)


def normalize_bundle(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("local Meme evidence response must be a JSON object")
    if str(value.get("version") or "") != INTERFACE_VERSION:
        raise RuntimeError("local Meme evidence interface version mismatch")
    status = str(value.get("status") or "")
    if status not in {"success", "partial", "empty", "error"}:
        raise RuntimeError("local Meme evidence response has invalid status")
    evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value.get("evidence") or []:
        if not isinstance(raw, dict):
            continue
        item_id = str(raw.get("id") or "").strip()
        source = str(raw.get("source") or "").strip()
        statement = str(raw.get("statement") or "").strip()
        if not item_id or item_id in seen_ids or source not in FAST_SOURCES or not statement:
            continue
        seen_ids.add(item_id)
        evidence.append({
            "id": item_id,
            "source": source,
            "statement": statement,
            "observed_at": str(raw.get("observedAt") or ""),
            "url": str(raw.get("url") or ""),
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        })
    return {**value, "status": status, "evidence": evidence}


def build_writer_prompt(*, chain: str, contract: str, symbol: str, materials: list[dict[str, Any]]) -> str:
    return f"""You are OdAIly's Chinese Meme narrative writer. Write `reader_text` for chain {chain}, symbol {symbol}, exact CA {contract}.

Only answer what concrete wording, event, identity, relationship, or assertion is being used to hype this token. Use only the supplied materials. Do not research or add background. Do not discuss price, market cap, volume, buying, selling, upside, truth, or investment value. Remove pure CA posts, bot cards, repetition counts, generic excitement, questions without an assertion, and vague slogans.

First silently choose primary_type, then write according to that type in the SAME call:
- pure_meme: begin with the concrete meme, wording, name, or event origin; explain the supplied hype logic. Do not invent a product definition.
- celebrity_anchor: begin with the named person/official account's concrete action and relevant quote/time, then its supplied community interpretation. A third party's claim is attributed to that source, never invented as an original action.
- app_linked: directly define what the project does, then explain the concrete platform/function/mechanism and why it is being hyped.
Only include recent actions when they develop or change the hype logic. Do not fill sections for completeness. No section headings.
Use clear subjects, precise active verbs and concrete objects/results. Compress chat text to its useful reason: e.g. `群聊 A 表示，多数用户亏钱，却仍被 Pump.fun 大额抽水。` Never narrate when a chat discussed the topic or posted the CA. Avoid `有材料提到`, `核心锚点`, abstract `把A接到B`, empty concluding commentary, and boilerplate uncertainty/disclaimers. Use material-supported causal relationships, do not invent them.

Keep source attribution where it matters:
- Telegram: one relevant chat is `群聊 A 表示…`; multiple independent chats are `多个群聊表示…`. Never expose chat titles or ordinary usernames.
- X: one source is `X 上有人表示…`; multiple independent sources are `X 上多名用户表示…`. Do not expose ordinary handles unless the account identity itself is the concrete narrative.
- grok_routine: use returned research directly as writing input without a second verification requirement or added "尚未核实" disclaimers. Preserve any explicit plan/rumor/speculation status in the returned content. Put its summarized hype reasons in a separate paragraph beginning EXACTLY `Grok补充资料显示，`; repetition of the main narrative is allowed. If usable Grok material is supplied, use it and classify its IDs as supplemental_information. Never fabricate this paragraph if Grok material is absent. Grok can be the sole usable source.
- fomo_thesis: always attribute every used claim with the exact wording `某信源表示…`. Never write `FOMO`, `Fomo`, `Thesis`, the source product name, author name, or account name in reader_text.

Silently classify each useful material as source, angle, or supplemental information. All three groups may be empty. Do not invent a missing angle or identity. Write concise, direct natural Chinese in as many concise paragraphs as the supplied information needs, without headings, lists, emoji, report language, fixed opening, or disclaimer. The program adds its opening and disclaimer after validation.

Return only JSON:
{{"primary_type":"pure_meme|celebrity_anchor|app_linked|","source_material_ids":[],"angle_material_ids":[],"supplemental_information_ids":[],"reader_text":"","used_material_ids":[],"discarded_material_ids":[]}}
Every id must come from the input. If there is no concrete usable narrative, reader_text must be empty.

Materials:
{json.dumps(materials, ensure_ascii=False)}"""


def write_json_with_metrics(
    prompt: str,
    *,
    model: str,
    timeout: int,
    base_url: str,
    api_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not base_url:
        raise RuntimeError("MEME_FAST_WRITER_BASE_URL is required")
    if not api_key:
        raise RuntimeError("MEME_FAST_WRITER_API_KEY is required")
    started = time.perf_counter()
    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Return only a valid JSON object without Markdown fences."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "reasoning_effort": "none",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Luna response has no chat-completion content") from exc
    if not isinstance(content, str):
        raise RuntimeError("Luna response content is not text")
    return (
        narrative_v2.extract_json_object(content, "Luna"),
        narrative_v2.performance_entry("", started, data),
    )


def run(
    args: Any,
    *,
    provider: FastEvidenceProvider,
) -> dict[str, Any]:
    started = time.perf_counter()
    output_path = Path(args.output) if args.output else Path(args.output_dir) / "fast-narrative.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    collection_started = time.perf_counter()
    try:
        bundle = normalize_bundle(
            provider.collect(
                chain=args.chain,
                contract=args.contract,
                symbol=getattr(args, "symbol", ""),
                request_id=f"odaily:meme:{args.chain}:{args.contract if args.chain == 'solana' else args.contract.lower()}",
            )
        )
    except Exception as exc:
        available = (getattr(args, "grok_routine", {}) or {}).get("result") or {}
        if available.get("ok") is not True:
            raise narrative_v2.NarrativeStageError("local_fast_evidence", exc) from exc
        bundle = {"status": "error", "evidence": [], "diagnostics": {"collection_error": type(exc).__name__}}

    collection_metric = {
        "stage": "local_fast_evidence",
        "duration_ms": round((time.perf_counter() - collection_started) * 1000),
        "status": bundle["status"],
    }
    grok_audit = getattr(args, "grok_routine", {}) or {}
    grok_result = grok_audit.get("result") or {}
    grok_materials = []
    if grok_audit.get("state") == "complete" and grok_result.get("ok") is True:
        for key in ("primary_narrative", "narratives", "summary"):
            if grok_result.get(key):
                grok_materials.append({"id": f"grok:routine:{key}", "source": "grok_routine",
                    "statement": json.dumps(grok_result[key], ensure_ascii=False), "metadata": {}})
    if bundle["status"] == "error" and not grok_materials:
        raise narrative_v2.NarrativeStageError(
            "local_fast_evidence",
            RuntimeError(str(bundle.get("decisionReason") or "local Meme evidence collection failed")),
        )

    materials = list(bundle["evidence"]) + grok_materials
    material_by_id = {item["id"]: item for item in materials}
    telegram_contexts = bundle.get("telegramContexts") if isinstance(bundle.get("telegramContexts"), list) else []
    telegram_messages = [item for item in materials if item["source"] == "telegram"]
    x_posts = [item for item in materials if item["source"] == "x"]
    fomo_materials = [item for item in materials if item["source"] == "fomo_thesis"]
    counts = {
        "telegram_messages": len(telegram_messages),
        "x_posts": len(x_posts),
        "fomo_materials": len(fomo_materials),
        "total_materials": len(materials),
    }
    writer_metric: dict[str, Any] = {"stage": "final_writer", "duration_ms": 0, "status": "skipped"}
    if not materials:
        final = narrative_v2.validate_final_result({}, material_by_id)
        status, decision_code, decision_reason = "empty", "no_materials", "三路快速信源均没有可用叙事材料"
    else:
        try:
            raw_final, writer_metric = write_json_with_metrics(
                build_writer_prompt(
                    chain=args.chain,
                    contract=args.contract,
                    symbol=getattr(args, "symbol", ""),
                    materials=materials,
                ),
                model=args.gpt_model,
                timeout=args.gpt_timeout,
                base_url=getattr(args, "writer_base_url", "") or os.getenv("ODAILY_LLM_BASE_URL") or "",
                api_key=getattr(args, "writer_api_key", "") or os.getenv("ODAILY_LLM_API_KEY") or "",
            )
        except Exception as exc:
            raise narrative_v2.NarrativeStageError("final_writer", exc) from exc
        writer_metric["stage"] = "final_writer"
        try:
            final = narrative_v2.validate_final_result(raw_final, material_by_id)
            used_fomo = {item["id"] for item in fomo_materials}.intersection(final["used_material_ids"])
            reader_text = str(final.get("reader_text") or "")
            if reader_text and not final.get("primary_type"):
                raise RuntimeError("Nonempty narrative requires a valid primary_type")
            if grok_materials and reader_text and (
                not re.search(r"(?m)^(?:" + re.escape(narrative_v2.READER_OPENING) + r")?Grok补充资料显示，", reader_text)
                or not any(item["id"] in final["used_material_ids"] for item in grok_materials)
                or not any(item.get("source") == "grok_routine" for item in final["supplemental_information"])
            ):
                raise RuntimeError("Usable Grok research must appear under Grok补充资料显示，")
            if used_fomo and ("某信源表示" not in reader_text or any(word in reader_text for word in ("FOMO", "Fomo", "Thesis"))):
                raise RuntimeError("FOMO material must use the anonymous `某信源表示` attribution")
        except Exception as exc:
            raise narrative_v2.NarrativeStageError("final_validation", exc) from exc
        if final["reader_text"]:
            status, decision_code, decision_reason = "success", "completed", "已由 Luna 基于快速信源生成叙事"
        else:
            status, decision_code, decision_reason = "empty", "writer_returned_empty", "Luna 未形成可核验的具体叙事"

    result = {
        "status": status,
        "failure_stage": None if status == "success" else ("final_writer" if decision_code == "writer_returned_empty" else "local_fast_evidence"),
        "failure_code": None if status == "success" else decision_code,
        "failure_message": None,
        "material_counts": counts,
        "decision_code": decision_code,
        "decision_reason": decision_reason,
        **final,
        "fast_evidence": bundle,
        "telegram_contexts": telegram_contexts,
        "telegram_messages": telegram_messages,
        "x_posts": x_posts,
        "fomo_materials": fomo_materials,
        "grok_research": grok_result,
        "grok_routine": grok_audit,
        "grok_diagnostics": [],
        "gmgn_supplement": [],
        "gmgn_diagnostic": {"stage": "gmgn_narrative", "status": "disabled"},
        "performance": {
            "total_duration_ms": round((time.perf_counter() - started) * 1000),
            "calls": [collection_metric, writer_metric],
        },
        "output_path": str(output_path),
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
