from __future__ import annotations

from packages.non_mainstream_media.models import NonMainstreamMediaSource, SourceRunStats
from packages.non_mainstream_media.repository import InMemoryNonMainstreamMediaRepository
from packages.non_mainstream_media.worker import NonMainstreamMediaWorker


def _source(source_id: int, site_key: str) -> NonMainstreamMediaSource:
    return NonMainstreamMediaSource(
        id=source_id,
        site_key=site_key,
        display_name=site_key,
        homepage_url=f"https://{site_key}.test",
        capture_method="html_request",
    )


def test_source_batch_emits_processing_heartbeats_between_sources() -> None:
    worker = NonMainstreamMediaWorker(repository=InMemoryNonMainstreamMediaRepository())
    sources = [_source(1, "first"), _source(2, "second")]
    progress: list[tuple[str, int]] = []

    worker._record_processing_heartbeat = lambda *, source, processed_items: progress.append(
        (source.site_key, processed_items)
    )  # type: ignore[method-assign]
    worker.process_source = lambda source: SourceRunStats(source=source, status="success")  # type: ignore[method-assign]

    stats = worker._process_sources(sources)

    assert [item.source.site_key for item in stats] == ["first", "second"]
    assert progress == [("first", 0), ("first", 1), ("second", 1), ("second", 2)]
