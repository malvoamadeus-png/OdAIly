from datetime import datetime, timedelta, timezone

from packages.meme_scanner.context_search import MessageRow, SearchHit, select_edge_hits


def hit(message_id: int, sent_at: datetime) -> SearchHit:
    return SearchHit(
        entity=None,
        row=MessageRow(
            chat_title="chat",
            chat_username=None,
            message_id=message_id,
            sent_at=sent_at,
            sender_name="sender",
            sender_username=None,
            text="0x1111111111111111111111111111111111111111",
        ),
        matched_terms=["0x"],
    )


def test_select_edge_hits_keeps_global_oldest_and_newest() -> None:
    start = datetime(2026, 8, 4, tzinfo=timezone.utc)
    hits = [hit(index, start + timedelta(minutes=index)) for index in range(1, 7)]

    selected = select_edge_hits(hits, newest_contexts=2, oldest_contexts=2)

    assert [item.row.message_id for item in selected] == [6, 5, 2, 1]
