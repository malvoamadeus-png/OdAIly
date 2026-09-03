from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from dotenv import load_dotenv

from packages.common.paths import get_paths

PATHS = get_paths()
CONFIG_DIR = PATHS.config_dir
EXPORTS_DATA_DIR = PATHS.exports_dir


def load_project_env() -> None:
    load_dotenv()


def read_secret_file(path: Path) -> str | None:
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


DEFAULT_API_KEY_FILE = CONFIG_DIR / "grok_api_key.txt"
DEFAULT_BASE_URL = "https://api.x.ai/v1"
# CLIProxyAPI's Premium+ OAuth route has been verified with X Search on this
# model. Callers may still choose another model explicitly.
DEFAULT_MODEL = "grok-4.5"
DEFAULT_OUTPUT = EXPORTS_DATA_DIR / "x_search" / "grok_x_search.json"


def setup_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def resolve_base_url(value: str | None) -> str:
    load_project_env()
    base_url = value or os.environ.get("GROK_BASE_URL")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
    return (base_url or DEFAULT_BASE_URL).rstrip("/")


def resolve_api_key(value: str | None, api_key_file: Path) -> str:
    load_project_env()
    api_key = value or os.environ.get("GROK_API_KEY") or read_secret_file(api_key_file)
    if not api_key:
        raise RuntimeError(
            "Missing Grok API key. Set GROK_API_KEY or create config/grok_api_key.txt."
        )
    return api_key


def build_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt
    target = args.contract or args.query
    if not target:
        raise RuntimeError("Provide --contract, --query, or --prompt.")
    prompt = (
        "Use X Search to research this crypto token or topic: "
        f"{target}. Start with the exact primary-source post that inspired the token. "
        "For each relevant post, return the author handle, exact post text, status URL or ID, "
        "timestamp, and interaction type (original, reply, quote, or repost). Source posts alone "
        "may be the complete answer. Include a human community post only when it adds a concrete "
        "claim beyond the source posts; do not include a restatement merely for completeness. Exclude "
        "scanner, bot, leaderboard, and automated ad posts. Exclude generic future outcomes, price "
        "talk, unsupported interaction expectations, and claims that an official mention will make a "
        "token rise. Do not report a missing interaction or missing proof unless it directly qualifies "
        "a specific collected human claim. Distinguish source facts from unsupported community claims "
        "and do not add your own explanation of why a meme should spread. Use only the text and "
        "relationships in X posts returned by X Search in this request. Do not supplement the answer "
        "with background knowledge, project websites, explorers, market-data pages, news, or other "
        "web sources. A cultural meme or historical event is usable only when a collected X post itself "
        "states the concrete historical detail and connects it to this token; a bare label such as "
        "'an old meme' is not usable. If the returned X posts contain only a CA, promotion, price talk, "
        "or an unsupported assertion, return an empty answer. Write sentences "
        "with the real actor as the subject, such as 'Binance posted...' or 'A community account "
        "said...'; never use Grok, X Search, search results, or available material as the subject. "
        "Return concise Chinese and preserve exact source wording."
    )
    if args.reader_text:
        prompt += (
            " Return only the final reader-facing narrative text. Do not include source URLs, headings, "
            "bullet lists, evidence labels, any explanation of the search process, verification language, "
            "or disclaimers such as 'this is not official' or 'there is no evidence'."
        )
    else:
        prompt += " Include source URLs."
    return prompt


def extract_output_text(payload: dict[str, Any]) -> str:
    """Return final Responses API message text, never reasoning/tool summaries."""
    message_pieces: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text") or content.get("output_text")
            if isinstance(text, str) and text.strip():
                message_pieces.append(text)
    if message_pieces:
        return "\n".join(dict.fromkeys(message_pieces))

    # Retain compatibility with older OpenAI-compatible proxies that omit
    # Responses API message typing, but prefer the unambiguous path above.
    pieces: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                pieces.append(value["text"])
            if isinstance(value.get("output_text"), str):
                pieces.append(value["output_text"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload.get("output", payload))
    return "\n".join(dict.fromkeys(piece for piece in pieces if piece.strip()))


def run(args: argparse.Namespace) -> int:
    setup_stdout()
    api_key = resolve_api_key(args.api_key, Path(args.api_key_file))
    base_url = resolve_base_url(args.base_url)
    prompt = build_prompt(args)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "model": args.model,
        "input": [{"role": "user", "content": prompt}],
        "tools": [{"type": "x_search"}],
    }
    response = requests.post(
        f"{base_url}/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=args.timeout,
    )
    raw_text = response.text
    output_path.write_text(raw_text, encoding="utf-8")
    print(f"HTTP status: {response.status_code}")
    print(f"Saved raw response: {output_path}")
    if response.status_code >= 400:
        print(raw_text[:2000])
        return 1

    data = response.json()
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": data.get("model"),
        "status": data.get("status"),
        "output_text": extract_output_text(data),
    }
    summary_path = output_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Model: {summary['model']}")
    print(f"Status: {summary['status']}")
    print(f"Saved summary: {summary_path}")
    if summary["output_text"]:
        print()
        print(summary["output_text"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Use Grok X Search through an OpenAI-compatible API.")
    parser.add_argument("--contract", help="Token contract address to research on X.")
    parser.add_argument("--query", help="Free-form X search research topic.")
    parser.add_argument("--prompt", help="Full prompt. Overrides --contract and --query.")
    parser.add_argument("--reader-text", action="store_true", help="Return only reader-facing narrative text, without source URLs or research labels.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", help="OpenAI-compatible base URL. Defaults to GROK_BASE_URL, OPENAI_BASE_URL, or xAI.")
    parser.add_argument("--api-key", help="Grok API key. Prefer GROK_API_KEY or config/grok_api_key.txt.")
    parser.add_argument("--api-key-file", default=str(DEFAULT_API_KEY_FILE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--timeout", type=int, default=90)
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
