from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from types import FrameType
from typing import Any, Iterator

from packages.common.config import WhaleWatchSettings
from packages.common.heartbeat import HeartbeatThrottle
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.x_processing.telegram import TelegramClient, skipped_telegram_result

from .chains import ChainDefinition, resolve_chains
from .client import BlockscoutClient
from .detector import detect_activity
from .models import Activity, WhaleRunResult
from .repository import WhaleWatchRepository


@dataclass(frozen=True, slots=True)
class CandidateTx:
    tx_hash: str
    block_number: int


class WhaleWatchWorker:
    def __init__(
        self,
        *,
        repository: WhaleWatchRepository,
        settings: WhaleWatchSettings,
        client: BlockscoutClient | None = None,
        telegram_client: TelegramClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.chains = resolve_chains(settings.chain_keys)
        self.client = client or BlockscoutClient(
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self.telegram_client = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self.worker_id = worker_id or f"whale_watch-{os.getpid()}"
        self.feed_writer = LocalEditorPluginFeedWriter()
        self._heartbeat = HeartbeatThrottle(
            component="whale_watch",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )
        self._stop = False

    def run_once(self) -> WhaleRunResult:
        addresses = self.repository.list_addresses(include_disabled=False)
        failed: dict[str, str] = {}
        processed_pairs = 0
        seeded_pairs = 0
        detected = 0
        inserted = 0
        sent = 0
        for whale in addresses:
            for chain in self.chains:
                processed_pairs += 1
                try:
                    stats = self._process_pair(whale=whale, chain=chain)
                    seeded_pairs += int(stats["seeded"])
                    detected += int(stats["detected"])
                    inserted += int(stats["inserted"])
                    sent += int(stats["sent"])
                except Exception as exc:
                    key = f"{whale.address_lower}:{chain.key}"
                    failed[key] = str(exc)
                    self.repository.record_chain_error(
                        address_id=whale.id,
                        chain_key=chain.key,
                        error=str(exc),
                        polled_at=_utc_now(),
                    )
                    print(f"[odaily] whale watch pair failed key={key} error={exc}")
        result = WhaleRunResult(
            addresses=len(addresses),
            chains=len(self.chains),
            processed_pairs=processed_pairs,
            seeded_pairs=seeded_pairs,
            detected=detected,
            inserted=inserted,
            sent=sent,
            failed=failed,
        )
        self._record_heartbeat(result)
        return result

    def run_forever(self) -> None:
        print(
            "[odaily] whale watch worker started. "
            f"chains={','.join(chain.key for chain in self.chains)} interval={self.settings.interval_seconds}s"
        )
        with self._install_signal_handlers():
            while not self._stop:
                try:
                    result = self.run_once()
                    print(
                        "[odaily] whale watch round "
                        f"addresses={result.addresses} pairs={result.processed_pairs} seeded={result.seeded_pairs} "
                        f"detected={result.detected} inserted={result.inserted} sent={result.sent} failed={len(result.failed)}"
                    )
                except Exception as exc:
                    print(f"[odaily] whale watch round failed: {exc}")
                self._sleep()

    def _process_pair(self, *, whale, chain: ChainDefinition) -> dict[str, int | bool]:
        polled_at = _utc_now()
        state = self.repository.get_chain_state(address_id=whale.id, chain_key=chain.key)
        transactions = self.client.list_address_transactions(chain, whale.address)
        transfers = self.client.list_address_token_transfers(chain, whale.address)
        candidates = _candidate_transactions(transactions, transfers)
        max_block = max((item.block_number for item in candidates), default=state.last_seen_block if state else None)
        if state is None or state.seeded_at is None:
            self.repository.mark_chain_seeded(
                address_id=whale.id,
                chain_key=chain.key,
                block_number=max_block,
                polled_at=polled_at,
            )
            return {"seeded": True, "detected": 0, "inserted": 0, "sent": 0}

        new_candidates = [
            item for item in sorted(candidates, key=lambda item: item.block_number)
            if state.last_seen_block is None or item.block_number > state.last_seen_block
        ]
        detected = 0
        inserted = 0
        sent = 0
        telegram_error: str | None = None
        transfers_by_tx = _group_transfers_by_tx(transfers)
        for candidate in new_candidates:
            tx = self.client.get_transaction(chain, candidate.tx_hash)
            activity = detect_activity(
                chain=chain,
                whale=whale,
                tx=tx,
                token_transfers=transfers_by_tx.get(candidate.tx_hash.lower()) or _transaction_transfers(tx),
            )
            if activity is None:
                continue
            detected += 1
            activity_id = self.repository.save_activity(whale=whale, chain_key=chain.key, activity=activity)
            if activity_id is None:
                continue
            inserted += 1
            self._write_activity_feed(activity_id=activity_id, whale=whale, chain_key=chain.key, activity=activity)
            sent_ok = self._send_activity(activity)
            sent += int(sent_ok)
            if not sent_ok:
                telegram_error = f"telegram send failed tx={activity.tx_hash}"
        self.repository.record_chain_success(
            address_id=whale.id,
            chain_key=chain.key,
            block_number=max_block,
            polled_at=polled_at,
        )
        if telegram_error:
            self.repository.record_chain_error(
                address_id=whale.id,
                chain_key=chain.key,
                error=telegram_error,
                polled_at=polled_at,
            )
        return {"seeded": False, "detected": detected, "inserted": inserted, "sent": sent}

    def _send_activity(self, activity: Activity) -> bool:
        result = skipped_telegram_result("whale business signal disabled; use editor plugin feed")
        self.repository.update_activity_telegram_result(
            tx_hash=activity.tx_hash,
            fingerprint=activity.fingerprint,
            telegram_result=result.model_dump(mode="json"),
        )
        if not result.ok and not result.skipped:
            print(f"[odaily] whale watch telegram failed tx={activity.tx_hash} error={result.error}")
        return result.ok or result.skipped

    def _write_activity_feed(self, *, activity_id: int, whale, chain_key: str, activity: Activity) -> None:
        try:
            self.feed_writer.upsert_whale_onchain(
                feed_item_id=f"whale_onchain:{activity_id}",
                address=whale.address,
                address_label=whale.label,
                chain_key=chain_key,
                activity_type=activity.kind,
                direction=activity.direction,
                summary=activity.summary or activity.telegram_text,
                tx_url=activity.tx_url,
                occurred_at=datetime.now(UTC),
            )
        except Exception as exc:
            print(f"[odaily] local feed write skipped whale_activity_id={activity_id} error={exc}")

    def _record_heartbeat(self, result: WhaleRunResult) -> None:
        try:
            self._heartbeat.send(
                status="ok" if not result.failed else "failed",
                success=not result.failed,
                error=str(result.failed) if result.failed else None,
                metadata={
                    "addresses": result.addresses,
                    "chains": result.chains,
                    "processed_pairs": result.processed_pairs,
                    "seeded_pairs": result.seeded_pairs,
                    "detected": result.detected,
                    "inserted": result.inserted,
                    "sent": result.sent,
                    "failed": result.failed,
                },
            )
        except Exception as exc:
            print(f"[odaily] whale watch heartbeat failed: {exc}")

    def _sleep(self) -> None:
        deadline = time.monotonic() + self.settings.interval_seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    @contextmanager
    def _install_signal_handlers(self) -> Iterator[None]:
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)

        def stop(_signum: int, _frame: FrameType | None) -> None:
            self._stop = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)


def _candidate_transactions(transactions: list[dict[str, Any]], transfers: list[dict[str, Any]]) -> list[CandidateTx]:
    by_hash: dict[str, CandidateTx] = {}
    for item in transactions:
        tx_hash = str(item.get("hash") or "").lower()
        block_number = _int(item.get("block_number") or item.get("blockNumber"))
        if tx_hash and block_number is not None:
            by_hash[tx_hash] = CandidateTx(tx_hash=tx_hash, block_number=block_number)
    for item in transfers:
        tx_hash = str(item.get("transaction_hash") or "").lower()
        block_number = _int(item.get("block_number"))
        if tx_hash and block_number is not None:
            by_hash[tx_hash] = CandidateTx(tx_hash=tx_hash, block_number=block_number)
    return sorted(by_hash.values(), key=lambda item: item.block_number, reverse=True)


def _group_transfers_by_tx(transfers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for transfer in transfers:
        tx_hash = str(transfer.get("transaction_hash") or "").lower()
        if tx_hash:
            grouped.setdefault(tx_hash, []).append(transfer)
    return grouped


def _transaction_transfers(tx: dict[str, Any]) -> list[dict[str, Any]]:
    transfers = tx.get("token_transfers")
    return [item for item in transfers if isinstance(item, dict)] if isinstance(transfers, list) else []


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)
