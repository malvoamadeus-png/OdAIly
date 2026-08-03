from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import requests

from packages.common.config import DEFAULT_GPT_WRITER_MODEL, DEFAULT_OPENAI_BASE_URL
from packages.x_processing.ai_client import OpenAIResponsesClient


def _output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _telegram_materials(path: Path, address: str, evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    messages = list((evidence or {}).get("messages") or [])
    if path.exists():
        try:
            with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT chat_id,message_id,sender_key,sent_at,text_excerpt
                    FROM ca_mentions WHERE address=? ORDER BY sent_at DESC LIMIT 30""",
                    (address,),
                ).fetchall()
            messages.extend(dict(row) for row in rows)
        except sqlite3.Error:
            pass
    deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, dict):
            deduped[(message.get("chat_id"), message.get("message_id"))] = message
    return list(deduped.values())[:30]


def _grok_material(address: str, symbol: str, timeout: int) -> str:
    api_key = os.getenv("MEME_GROK_API_KEY") or os.getenv("GROK_API_KEY")
    base_url = (os.getenv("MEME_GROK_BASE_URL") or os.getenv("GROK_BASE_URL") or "").rstrip("/")
    if not api_key or not base_url:
        return ""
    prompt = (
        f"Search X for current, source-backed discussion of BSC token {symbol} with contract {address}. "
        "Return concise Chinese research notes. Identify who said what and preserve uncertainty. "
        "Do not invent an official relationship, origin story, endorsement, or wallet attribution."
    )
    response = requests.post(
        f"{base_url}/responses",
        json={
            "model": os.getenv("MEME_GROK_MODEL") or "grok-4.1-fast",
            "input": prompt,
            "tools": [{"type": "x_search"}],
        },
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return _output_text(response.json())


def _fallback_reader_text(messages: list[dict[str, Any]], grok_text: str, trigger_kind: str) -> str:
    if grok_text:
        return " ".join(grok_text.split())[:600]
    if not messages:
        return ""
    if trigger_kind == "tg_burst":
        return "Telegram 白名单社群中，多名用户正在围绕该代币展开讨论。"
    return "Telegram 白名单社群中已有用户提及该代币。"


def _llm_settings() -> dict[str, Any]:
    base_url = os.getenv("ODAILY_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
    return {
        "api_key": os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
        "base_url": base_url,
        "api_style": os.getenv("ODAILY_LLM_API_STYLE") or ("chat_completions" if os.getenv("ODAILY_LLM_BASE_URL") else "responses"),
        "model": os.getenv("MEME_WRITER_MODEL") or (DEFAULT_GPT_WRITER_MODEL if os.getenv("ODAILY_LLM_BASE_URL") else "gpt-5.5"),
    }


def generate_reader_text(
    *,
    address: str,
    symbol: str,
    trigger_kind: str,
    database_path: Path,
    evidence: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    messages = _telegram_materials(database_path, address, evidence)
    try:
        grok_text = _grok_material(address, symbol, timeout)
        grok_error = None
    except Exception as exc:
        grok_text = ""
        grok_error = str(exc)
    if not messages and not grok_text:
        return {"reader_text": "", "telegram_messages": [], "grok_text": "", "grok_error": grok_error}

    settings = _llm_settings()
    prompt = (
        "你是 OdAIly 快讯编辑。根据下列已保存材料写 1 到 2 句中文新闻正文，只陈述材料支持的事实。"
        "不要复述市值、链、CA 或标题外壳；不要写风险提示、投资建议、监控过程、发射时长；"
        "不要把社区说法写成官方事实。直接输出正文，不要 Markdown。\n\n"
        f"代币：{symbol}\nCA：{address}\n触发类型：{trigger_kind}\n"
        f"Telegram 材料：{json.dumps(messages, ensure_ascii=False)}\n"
        f"Grok X Search 材料：{grok_text or '无'}"
    )
    reader_text = ""
    if settings["api_key"]:
        try:
            client = OpenAIResponsesClient(
                api_key=settings["api_key"],
                base_url=settings["base_url"],
                api_style=settings["api_style"],
                timeout_seconds=float(timeout),
                max_attempts=2,
                backoff_seconds=1,
            )
            reader_text = client.generate_text(model=settings["model"], prompt=prompt).strip()
        except Exception:
            reader_text = ""
    if not reader_text:
        reader_text = _fallback_reader_text(messages, grok_text, trigger_kind)
    return {
        "reader_text": reader_text,
        "telegram_messages": messages,
        "grok_text": grok_text,
        "grok_error": grok_error,
    }
