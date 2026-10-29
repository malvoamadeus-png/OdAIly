from __future__ import annotations

import json
from pathlib import Path

from packages.business_news.repository import BusinessNewsRepository
from packages.business_news.worker import (
    BusinessNewsProcessor,
    business_telegram_bot_token,
    count_non_whitespace_without_urls,
    extract_urls,
)
from packages.publisher.push_client import PushResult


def _message_update(
    update_id: int,
    message_id: int,
    *,
    chat_id: int = -100123,
    sender_id: int = 77,
    text: str | None = "这是超过十五个字符的一条商务消息",
    chat_title: str = "SoSo Value 群",
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1793275200,
        "chat": {"id": chat_id, "type": "supergroup", "title": chat_title},
        "from": {"id": sender_id, "first_name": "Soso"},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


def _enable_group(repository: BusinessNewsRepository, update: dict) -> str:
    discovered = repository.ingest_update(update, source_urls=[], own_bot_id="999")
    assert discovered is not None
    chat_id = discovered["chat_id"]
    repository.save_group(chat_id=chat_id, customer_name="Soso", enabled=True)
    return chat_id


def test_text_gate_counts_unicode_without_whitespace_or_urls() -> None:
    assert count_non_whitespace_without_urls("中文 文字 https://example.com/x") == 4
    assert count_non_whitespace_without_urls("中文消息abc123") == 10
    assert extract_urls("发布详情：https://example.com/news。") == ["https://example.com/news"]
    assert extract_urls("打开说明", [{"type": "text_link", "url": "https://example.com/ref"}]) == [
        "https://example.com/ref"
    ]


def test_business_bot_token_prefers_dedicated_token(monkeypatch) -> None:
    monkeypatch.setenv("BUSINESS_NEWS_TELEGRAM_BOT_TOKEN", "business-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "shared-token")

    assert business_telegram_bot_token() == "business-token"


def test_business_bot_token_falls_back_to_shared_token(monkeypatch) -> None:
    monkeypatch.delenv("BUSINESS_NEWS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " shared-token ")

    assert business_telegram_bot_token() == "shared-token"


def test_startup_backfill_registers_group_but_does_not_store_old_message(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    old_update = _message_update(101, 31)

    discovered = repository.ingest_update(
        old_update,
        source_urls=[],
        own_bot_id="999",
        startup_backfill=True,
    )

    assert discovered and discovered["newly_discovered"]
    assert repository.last_update_id() == 101
    dashboard = repository.list_dashboard({})
    assert dashboard["groups"][0]["chat_id"] == "-100123"
    assert dashboard["groups"][0]["is_enabled"] == 0
    assert dashboard["items"] == []


def test_update_cursor_is_scoped_to_bot_id(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    repository.ingest_update(
        _message_update(101, 31),
        source_urls=[],
        own_bot_id="999",
        bot_id="bot-one",
    )

    assert repository.last_update_id("bot-one") == 101
    assert repository.last_update_id("bot-two") is None
    assert repository.last_update_id() is None


def test_short_and_non_text_messages_are_filtered_before_ai(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    _enable_group(repository, _message_update(1, 1, text="群组介绍消息"))
    repository.ingest_update(
        _message_update(2, 2, text="12345678901234"),
        source_urls=[],
        own_bot_id="999",
    )
    repository.ingest_update(
        _message_update(3, 3, text="123456789012345"),
        source_urls=[],
        own_bot_id="999",
    )
    repository.ingest_update(
        _message_update(4, 4, text=None),
        source_urls=[],
        own_bot_id="999",
    )

    dashboard = repository.list_dashboard({})

    assert [(item["status"], item["raw_text"]) for item in dashboard["items"]] == [
        ("skipped_non_text", None),
        ("pending", "123456789012345"),
        ("filtered_short", None),
    ]
    assert len(repository.claim_pending(4)) == 1


def test_adjacent_url_uses_same_sender_and_only_immediate_neighbor(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    chat_id = _enable_group(repository, _message_update(1, 100, text="加入原文链接 https://example.com/source"))
    repository.ingest_update(
        _message_update(2, 101, text="下一条是不同发送者的普通内容", sender_id=88),
        source_urls=[],
        own_bot_id="999",
    )
    current = _message_update(3, 102, text="这是一条达到十五个字符的商务消息")
    repository.ingest_update(current, source_urls=[], own_bot_id="999")
    candidate = next(item for item in repository.claim_pending(5) if item["message_id"] == 102)
    assert candidate["chat_id"] == chat_id
    previous_urls, next_urls = repository.adjacent_urls(candidate)
    assert previous_urls == []
    assert next_urls == []


class _FakeAI:
    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload
        self.calls = 0

    def generate_text(self, **kwargs) -> str:
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


class _FakePush:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict] = []

    def push(self, **kwargs) -> PushResult:
        self.calls.append(kwargs)
        return PushResult(ok=self.ok, status_code=200 if self.ok else 503, response_text="ok")


class _FakeFeed:
    def __init__(self) -> None:
        self.notices: list[dict] = []

    def upsert_business_news_notice(self, **kwargs) -> None:
        self.notices.append(kwargs)


def test_newsflash_uses_writer2_pushes_as_background_and_notifies_once(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    update = _message_update(10, 20, text="Gate 上线 Ritual 现货交易并推出相关 CandyDrop 奖池安排")
    _enable_group(repository, _message_update(9, 19, text="群组介绍消息"))
    repository.ingest_update(update, source_urls=["https://gate.com/news"], own_bot_id="999")
    item = repository.claim_pending(1)[0]
    ai = _FakeAI(
        {
            "classification": "newsflash",
            "reason": "Ritual 上线是可独立报道的产品事件。",
            "writing_style": "plain",
            "title": "Gate 将上线 Ritual 现货交易并推出 CandyDrop",
            "content": "Gate 将上线 Ritual 现货交易。CandyDrop 将推出相关奖励。",
        }
    )
    push = _FakePush()
    feed = _FakeFeed()
    processor = BusinessNewsProcessor(
        repository=repository,
        ai_client=ai,
        model="odaily-gpt-writer",
        push_client=push,  # type: ignore[arg-type]
        feed_writer=feed,  # type: ignore[arg-type]
    )

    processor.process(item)

    row = repository.list_dashboard({})["items"][0]
    assert row["status"] == "backgrounded"
    assert row["classification"] == "newsflash"
    assert row["writer2_title"] == "Gate将上线Ritual现货交易并推出CandyDrop"
    assert row["source_url"] == "https://gate.com/news"
    assert ai.calls == 1
    assert len(push.calls) == 1
    assert push.calls[0]["dry_run"] is False
    assert push.calls[0]["is_publish"] is False
    assert push.calls[0]["is_push"] is False
    assert len(feed.notices) == 1
    assert feed.notices[0]["customer_name"] == "Soso"


def test_activity_is_retained_without_push_or_plugin_notice(tmp_path: Path) -> None:
    repository = BusinessNewsRepository(tmp_path / "main.sqlite")
    _enable_group(repository, _message_update(1, 1, text="群组介绍消息"))
    repository.ingest_update(
        _message_update(2, 2, text="活动期间完成交易任务可参与抽奖并领取指定奖励"),
        source_urls=[],
        own_bot_id="999",
    )
    item = repository.claim_pending(1)[0]
    push = _FakePush()
    feed = _FakeFeed()
    processor = BusinessNewsProcessor(
        repository=repository,
        ai_client=_FakeAI(
            {
                "classification": "activity",
                "reason": "内容要求完成交易任务并参与抽奖。",
                "writing_style": "none",
                "title": "",
                "content": "",
            }
        ),
        model="odaily-gpt-writer",
        push_client=push,  # type: ignore[arg-type]
        feed_writer=feed,  # type: ignore[arg-type]
    )

    processor.process(item)

    row = repository.list_dashboard({})["items"][0]
    assert row["status"] == "activity_pending_api"
    assert row["raw_text"] == "活动期间完成交易任务可参与抽奖并领取指定奖励"
    assert push.calls == []
    assert feed.notices == []
