from __future__ import annotations

from datetime import datetime

from packages.writer3.backfill import extract_list, fetch_odaily_page, has_more, parse_odaily_item

from .models import ODAILY_REFERENCE_SOURCE
from .searcher import SearchDocument


def fetch_odaily_reference_documents_from_api(
    *,
    since: datetime,
    timeout_seconds: float,
    page_size: int = 100,
    max_pages: int = 10,
) -> list[SearchDocument]:
    documents: list[SearchDocument] = []
    seen_ids: set[str] = set()
    page = 1
    while page <= max_pages:
        payload = fetch_odaily_page(page=page, size=page_size, timeout_seconds=timeout_seconds)
        items = [parse_odaily_item(item) for item in extract_list(payload)]
        items = [item for item in items if item is not None]
        if not items:
            break
        for item in items:
            if item.published_at is not None and item.published_at < since:
                continue
            if item.source_item_id in seen_ids:
                continue
            seen_ids.add(item.source_item_id)
            documents.append(
                SearchDocument(
                    doc_type="odaily_reference",
                    doc_id=item.source_item_id,
                    title=item.title,
                    content=item.content,
                    source=ODAILY_REFERENCE_SOURCE,
                    source_url=item.source_url,
                    published_at=item.published_at,
                    status="published",
                    metadata={**item.metadata, "reference_source": "odaily_api"},
                )
            )
        oldest = min((item.published_at for item in items if item.published_at), default=None)
        if oldest is not None and oldest < since:
            break
        if not has_more(payload):
            break
        page += 1
    return documents
