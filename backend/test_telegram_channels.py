import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel

from packages.console_data_api import ConsoleDataApi
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.editor_plugin_local_store import LocalEditorPluginStore
from packages.telegram_channels import TelegramChannelStore, deliver_message
from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import build_writer_prompt, resolve_publisher_channel


def test_personal_message_creates_one_retryable_task_and_can_be_claimed(tmp_path: Path) -> None:
    path = tmp_path / "main.sqlite"
    processing = SQLiteXProcessingRepository(path)
    store = TelegramChannelStore(path)
    store.init_schema()
    message = SimpleNamespace(id=278, raw_text="硬件股会持有几个月", date=datetime.now(UTC))
    entity = Channel(id=2631914757, title="投机之路", photo=None, date=datetime.now(UTC))
    class Pipeline:
        calls = []
        def submit_job(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("queue unavailable")
    pipeline = Pipeline()
    feed = LocalEditorPluginFeedWriter(LocalEditorPluginStore(tmp_path / "feed.sqlite"))
    with pytest.raises(RuntimeError, match="queue unavailable"):
        asyncio.run(deliver_message("vida", entity, message, store=store, feed=feed, pipeline=pipeline))
    assert store.cursor("vida") is None
    asyncio.run(deliver_message("vida", entity, message, store=store, feed=feed, pipeline=pipeline))
    assert pipeline.calls[0]["task_id"] == pipeline.calls[1]["task_id"]
    task = processing.claim_task_by_id("judge_crypto", task_id=pipeline.calls[1]["task_id"], worker_id="test")
    assert task is not None
    assert task.source == "telegram_channel"
    assert task.source_url == "https://t.me/c/2631914757/278"
    assert task.metadata["effective_author_name"] == "方程式新闻创始人Vida"
    assert resolve_publisher_channel(task) == "x"


def test_telegram_prompt_overrides_x_attribution() -> None:
    task = TaskRecord(id=1, source="telegram_channel", source_item_id="vida:1", source_url=None,
                      title="Vida", content="原文", metadata={"effective_author_name": "Vida"})
    template = PromptTemplateVersion(id=1, template_key="x_regular_writer", version_number=1,
                                     content="正文开头应为“{发言人}在X平台发文表示”")
    prompt = build_writer_prompt(task=task, prompt=template)
    assert "{发言人}于个人频道表示" in prompt
    assert "{发言人}在X平台发文表示" not in prompt
    assert "发布人：Vida" in prompt


def test_onchain_template_does_not_force_monitoring_attribution() -> None:
    task = TaskRecord(id=2, source="telegram_channel", source_item_id="vida:2", source_url=None,
                      title="Vida", content="原文", metadata={"effective_author_name": "Vida"})
    template = PromptTemplateVersion(id=2, template_key="x_onchain_writer", version_number=1,
                                     content="据{发言人}监测，")
    prompt = build_writer_prompt(task=task, prompt=template)
    assert "正文必须以‘{发言人}于个人频道表示，’开头" in prompt
    assert "当前任务已匹配链上模板" not in prompt


def test_console_only_allows_names_on_existing_personal_channels(tmp_path: Path) -> None:
    path = tmp_path / "main.sqlite"
    TelegramChannelStore(path).init_schema()
    api = ConsoleDataApi(path)
    api.execute({"table": "telegram_channel_names", "operation": "update", "data": {"write_name": "Vida"},
                 "filters": [{"column": "channel_key", "op": "eq", "value": "vida"}]})
    with pytest.raises(ValueError):
        api.execute({"table": "telegram_channel_names", "operation": "insert", "data": {"channel_key": "new", "write_name": "x"}})
