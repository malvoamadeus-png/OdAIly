"""Fast Meme narrative: HideOnBush evidence in, OdAIly Luna prose out."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import requests

from . import narrative_v2


INTERFACE_VERSION = "2026-09-01"
FAST_SOURCES = frozenset({"telegram", "x", "fomo_thesis"})


class FastEvidenceProvider(Protocol):
    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HTTPFastEvidenceAdapter:
    endpoint: str
    internal_key: str
    timeout_seconds: int = 45

    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]:
        if not self.endpoint:
            raise RuntimeError("MEME_FAST_EVIDENCE_URL is required")
        if len(self.internal_key) < 32:
            raise RuntimeError("MEME_FAST_EVIDENCE_INTERNAL_KEY must contain at least 32 characters")
        request = Request(
            self.endpoint,
            data=json.dumps({
                "chain": chain,
                "contract": contract,
                "symbol": symbol,
                "requestId": request_id,
            }).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Internal-Key": self.internal_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"HideOnBush evidence returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"HideOnBush evidence request failed: {exc}") from exc
        return normalize_bundle(payload)


@dataclass(frozen=True)
class InMemoryFastEvidenceAdapter:
    bundle: dict[str, Any]

    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]:
        del chain, contract, symbol, request_id
        return normalize_bundle(self.bundle)


def normalize_bundle(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("HideOnBush evidence response must be a JSON object")
    if str(value.get("version") or "") != INTERFACE_VERSION:
        raise RuntimeError("HideOnBush evidence interface version mismatch")
    status = str(value.get("status") or "")
    if status not in {"success", "partial", "empty", "error"}:
        raise RuntimeError("HideOnBush evidence response has invalid status")
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

Keep source attribution because these are third-party claims, not established facts:
- Telegram: one relevant chat is `群聊 A 表示…`; multiple independent chats are `多个群聊表示…`. Never expose chat titles or ordinary usernames.
- X: one source is `X 上有人表示…`; multiple independent sources are `X 上多名用户表示…`. Do not expose ordinary handles unless the account identity itself is the concrete narrative.
- fomo_thesis: always attribute every used claim with the exact wording `某信源表示…`. Never write `FOMO`, `Fomo`, `Thesis`, the source product name, author name, or account name in reader_text.

Silently classify each useful material as source, angle, or supplemental information. All three groups may be empty. Do not invent a missing angle or identity. Write concise, direct natural Chinese in one or two paragraphs, without headings, lists, emoji, report language, fixed opening, or disclaimer. The program adds its opening and disclaimer after validation.

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
        bundle = provider.collect(
            chain=args.chain,
            contract=args.contract,
            symbol=getattr(args, "symbol", ""),
            request_id=f"odaily:meme:{args.chain}:{args.contract.lower()}",
        )
    except Exception as exc:
        raise narrative_v2.NarrativeStageError("hideonbush_fast_evidence", exc) from exc
    collection_metric = {
        "stage": "hideonbush_fast_evidence",
        "duration_ms": round((time.perf_counter() - collection_started) * 1000),
        "status": bundle["status"],
    }
    if bundle["status"] == "error":
        raise narrative_v2.NarrativeStageError(
            "hideonbush_fast_evidence",
            RuntimeError(str(bundle.get("decisionReason") or "HideOnBush evidence collection failed")),
        )

    materials = list(bundle["evidence"])
    material_by_id = {item["id"]: item for item in materials}
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
        "failure_stage": None if status == "success" else ("final_writer" if decision_code == "writer_returned_empty" else "hideonbush_fast_evidence"),
        "failure_code": None if status == "success" else decision_code,
        "failure_message": None,
        "material_counts": counts,
        "decision_code": decision_code,
        "decision_reason": decision_reason,
        **final,
        "fast_evidence": bundle,
        "telegram_contexts": [],
        "telegram_messages": telegram_messages,
        "x_posts": x_posts,
        "fomo_materials": fomo_materials,
        "grok_research": {},
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
