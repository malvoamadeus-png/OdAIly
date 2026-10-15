"""Generate local editorial previews from reviewed account-loss fixtures, no database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from packages.common.config import load_x_processing_settings
from packages.hyperliquid_news.preview import generate_preview
from packages.hyperliquid_news.account_loss import loss_qualified
from packages.hyperliquid_news.models import Rules
from packages.x_processing.ai_client import OpenAIResponsesClient
from packages.x_processing.models import HYPERLIQUID_SOURCE, PromptTemplateVersion, TaskRecord


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.input.read_text(encoding='utf-8'))
    settings = load_x_processing_settings()
    client = OpenAIResponsesClient(api_key=settings.openai_api_key,
        base_url=str(settings.openai_base_url), api_style=settings.openai_api_style,
        timeout_seconds=settings.writer_request_timeout_seconds,
        max_attempts=2, backoff_seconds=1)
    prompt = PromptTemplateVersion(id=0, template_key='hyperliquid_writer', version_number=0,
        content=(ROOT / 'docs/OdAIly代码与整体设计/Hyperliquid快讯模板.txt').read_text(encoding='utf-8'))
    results = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        event = case['event']
        if not event.get('evidence', {}).get('test_replay', {}).get('real_public_data'):
            raise ValueError('Editorial acceptance requires verified public-data replay cases')
        if not loss_qualified(event['evidence'].get('history', {}).get('official_account_loss', {}), Rules()):
            raise ValueError('Official allTime account total loss does not meet the threshold')
        task = TaskRecord(id=0, source=HYPERLIQUID_SOURCE, source_item_id=event['key'],
            source_url='', title=None, content='', raw_payload=event)
        preview = generate_preview(task=task, prompt=prompt, settings=settings,
            ai_client=client, prompt_source='repository_template')
        results.append({**case, 'preview': preview})
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(preview['title'], flush=True)


if __name__ == '__main__':
    main()
