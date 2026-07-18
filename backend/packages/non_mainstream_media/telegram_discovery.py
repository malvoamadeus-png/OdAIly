from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

try:
    import socks
except ModuleNotFoundError:
    socks = None

from packages.common.config import TelegramDiscoverySettings
from packages.common.heartbeat import HeartbeatThrottle
from packages.common.source_exclusions import SourceExclusionMatcher, media_source_exclusion_scopes
from packages.local_pipeline.client import LocalPipelineClient

from .fetcher import fetch_article, get_site_registry
from .models import (
    DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK,
    DiscoveredPage,
    NonMainstreamMediaSource,
    ParsedArticle,
    SiteDefinition,
)
from .repository import NonMainstreamMediaRepository, alert_only_task_source, write_flow_task_source


PROXY_TYPES = {
    "socks5": "SOCKS5",
    "socks5h": "SOCKS5",
    "socks4": "SOCKS4",
    "http": "HTTP",
    "https": "HTTP",
}

TARGET_SITES = {
    "coindesk": {
        "prefixes": ["coindesk:", "[coindesk]"],
        "domains": ["coindesk.com", "www.coindesk.com"],
    },
    "cointelegraph": {
        "prefixes": ["cointelegraph:", "[cointelegraph]"],
        "domains": ["cointelegraph.com", "www.cointelegraph.com"],
    },
    "decrypt": {
        "prefixes": ["decrypt:", "[decrypt]"],
        "domains": ["decrypt.co", "www.decrypt.co"],
    },
    "the_block": {
        "prefixes": ["the block:", "[the block]", "theblock:", "[theblock]"],
        "domains": ["theblock.co", "www.theblock.co"],
    },
}

URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
REGISTRY_SYNC_INTERVAL_SECONDS = 300.0


@dataclass(slots=True)
class RetryTask:
    source: NonMainstreamMediaSource
    site_key: str
    url: str
    message_id: int
    retry_attempt: int
    next_attempt_monotonic: float


def parse_proxy_url(value: str) -> tuple[int, str, int, bool, str | None, str | None]:
    if socks is None:
        raise RuntimeError("telegram discovery requires PySocks")
    parsed = urlparse(value if "://" in value else f"socks5://{value}")
    scheme = parsed.scheme.lower()
    if scheme not in PROXY_TYPES:
        raise ValueError(f"Unsupported proxy scheme {scheme!r}")
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"Proxy must include host and port: {value}")
    return (
        getattr(socks, PROXY_TYPES[scheme]),
        parsed.hostname,
        parsed.port,
        True,
        parsed.username,
        parsed.password,
    )


def windows_system_proxy() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    script = (
        "Get-ItemProperty -Path "
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
        "| Select-Object -ExpandProperty ProxyEnable; "
        "Get-ItemProperty -Path "
        "'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
        "| Select-Object -ExpandProperty ProxyServer"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] not in {"1", "True", "true"}:
        return None
    proxy_server = lines[1]
    if "=" in proxy_server:
        entries = {}
        for part in proxy_server.split(";"):
            if "=" in part:
                key, entry_value = part.split("=", 1)
                entries[key.strip().lower()] = entry_value.strip()
        proxy_server = entries.get("socks") or entries.get("https") or entries.get("http") or ""
    if not proxy_server:
        return None
    if "://" not in proxy_server:
        proxy_server = f"socks5://{proxy_server}"
    return proxy_server


def resolve_proxy(value: str | None) -> tuple[int, str, int, bool, str | None, str | None] | None:
    if value == "none":
        return None
    proxy_url = value
    if value == "auto":
        proxy_url = windows_system_proxy()
    if not proxy_url:
        return None
    return parse_proxy_url(proxy_url)


def normalize_channel_target(value: str) -> str:
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.scheme and parsed.netloc.endswith("t.me"):
        path = parsed.path.strip("/")
        if not path:
            raise ValueError(f"Channel URL has no username/path: {value}")
        return path.split("/", 1)[0]
    return stripped.lstrip("@")


def message_text(message: Any) -> str:
    value = getattr(message, "message", None)
    if isinstance(value, str) and value:
        return value
    value = getattr(message, "text", None)
    return value if isinstance(value, str) else ""


def extract_message_title(text: str, site_key: str, url: str | None = None) -> str | None:
    normalized = text.strip()
    if not normalized:
        return None
    if url:
        escaped_url = re.escape(url)
        for pattern in (
            rf"(?is)(?:\]\({escaped_url}\)\s*\({escaped_url}\)|\({escaped_url}\)|{escaped_url})\s*:\s*(?P<title>.+)$",
            rf"(?is)(?:\]\({escaped_url}\)|{escaped_url})\s*:\s*(?P<title>.+)$",
        ):
            matched = re.search(pattern, normalized)
            if matched:
                extracted = matched.group("title").strip(" -:()[]\n\t")
                if extracted:
                    return extracted
    lowered = normalized.lower()
    for prefix in TARGET_SITES[site_key]["prefixes"]:
        if lowered.startswith(prefix):
            normalized = normalized[len(prefix) :].strip()
            break
    if url:
        normalized = normalized.replace(url, " ")
    normalized = re.sub(r"\(\s*\)", " ", normalized)
    normalized = re.sub(r"\[\s*\]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" -:()[]\n\t")
    if not normalized:
        return None
    if url and normalized.rstrip("/") == url.rstrip("/"):
        return None
    return normalized


def extract_site_key(text: str) -> str | None:
    normalized = text.strip().lower()
    for site_key, spec in TARGET_SITES.items():
        if any(normalized.startswith(prefix) for prefix in spec["prefixes"]):
            return site_key
    return None


def unwrap_google_news_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc not in {"news.google.com", "www.news.google.com"}:
        return url
    query_url = parse_qs(parsed.query).get("url")
    return query_url[0] if query_url and query_url[0] else url


def normalize_candidate_url(url: str) -> str:
    candidate = url.strip()
    if candidate.startswith("www."):
        candidate = f"https://{candidate}"
    return unwrap_google_news_url(candidate.rstrip(").,;!?'\""))


def host_matches_site(url: str, site_key: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in TARGET_SITES[site_key]["domains"])


def extract_candidate_urls_from_entities(message: Any) -> list[str]:
    text = message_text(message)
    candidates: list[str] = []
    for entity in list(getattr(message, "entities", None) or []):
        entity_name = entity.__class__.__name__
        if entity_name == "MessageEntityTextUrl":
            url = getattr(entity, "url", None)
            if url:
                candidates.append(str(url))
            continue
        if entity_name == "MessageEntityUrl":
            offset = int(getattr(entity, "offset", 0) or 0)
            length = int(getattr(entity, "length", 0) or 0)
            if length > 0:
                value = text[offset : offset + length].strip()
                if value:
                    candidates.append(value)
    return candidates


def extract_candidate_urls_from_media(message: Any) -> list[str]:
    candidates: list[str] = []
    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None) if media is not None else None
    for attr_name in ("url", "display_url"):
        value = getattr(webpage, attr_name, None) if webpage is not None else None
        if value:
            candidates.append(str(value))
    return candidates


def extract_candidate_url(message: Any, site_key: str) -> str | None:
    raw_candidates: list[str] = []
    raw_candidates.extend(extract_candidate_urls_from_entities(message))
    raw_candidates.extend(extract_candidate_urls_from_media(message))
    raw_candidates.extend(URL_RE.findall(message_text(message)))
    seen: set[str] = set()
    for raw in raw_candidates:
        candidate = normalize_candidate_url(raw)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if host_matches_site(candidate, site_key):
            return candidate
    return None


def should_retry_for_error_message(message: str | None) -> bool:
    normalized = (message or "").lower()
    return any(
        token in normalized
        for token in (
            "404",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "body is empty",
            "content is empty",
        )
    )


class TelegramDiscoveryWorker:
    def __init__(
        self,
        *,
        repository: NonMainstreamMediaRepository,
        settings: TelegramDiscoverySettings,
        pipeline_client: LocalPipelineClient | None = None,
        site_registry: dict[str, SiteDefinition] | None = None,
        fetch_article_fn: Callable[..., ParsedArticle] = fetch_article,
        request_timeout_seconds: float | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        clock: Callable[[], float] | None = None,
        exclusion_matcher: SourceExclusionMatcher | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.pipeline_client = pipeline_client
        self.site_registry = site_registry or get_site_registry()
        self.fetch_article_fn = fetch_article_fn
        self.request_timeout_seconds = (
            float(request_timeout_seconds)
            if request_timeout_seconds is not None
            else float(settings.timeout_seconds)
        )
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self._clock = clock or time.monotonic
        self.exclusion_matcher = exclusion_matcher
        self.worker_id = f"non_mainstream_media_telegram-{os.getpid()}"
        self.channel_name = normalize_channel_target(settings.channel)
        self._last_message_id = 0
        self._retry_tasks: dict[str, RetryTask] = {}
        self._last_registry_sync_monotonic: float | None = None
        self._heartbeat = HeartbeatThrottle(
            component="non_mainstream_media_telegram",
            worker_id=self.worker_id,
            interval_seconds=max(30.0, float(settings.poll_interval_seconds)),
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )

    def _now(self) -> float:
        return float(self._clock())

    def _maybe_sync_registry(self, *, force: bool = False) -> None:
        now = self._now()
        if (
            not force
            and self._last_registry_sync_monotonic is not None
            and now - self._last_registry_sync_monotonic < REGISTRY_SYNC_INTERVAL_SECONDS
        ):
            return
        self.repository.sync_sources(list(self.site_registry.values()))
        self._last_registry_sync_monotonic = now

    def _telegram_sources(self) -> dict[str, NonMainstreamMediaSource]:
        sources = self.repository.list_sources(include_disabled=False)
        return {
            source.site_key: source
            for source in sources
            if source.discovery_mode == DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK
            and source.site_key in TARGET_SITES
            and source.site_key in self.site_registry
        }

    def _retry_key(self, site_key: str, url: str) -> str:
        return f"{site_key}::{url}"

    def _schedule_retry(
        self,
        *,
        source: NonMainstreamMediaSource,
        site_key: str,
        url: str,
        message_id: int,
        retry_attempt: int,
    ) -> None:
        if retry_attempt > self.settings.retry_max_attempts:
            return
        self._retry_tasks[self._retry_key(site_key, url)] = RetryTask(
            source=source,
            site_key=site_key,
            url=url,
            message_id=message_id,
            retry_attempt=retry_attempt,
            next_attempt_monotonic=self._now() + self.settings.retry_delay_seconds,
        )

    def _clear_retry(self, site_key: str, url: str) -> None:
        self._retry_tasks.pop(self._retry_key(site_key, url), None)

    def _build_page(self, url: str, *, title: str | None = None) -> DiscoveredPage:
        return DiscoveredPage(
            source_item_id=url,
            detail_url=url,
            title=title,
            excerpt=None,
            published_at=None,
            published_at_raw=None,
        )

    def _submit_pipeline_job(self, *, task_id: int, source: str, source_item_id: str) -> None:
        if self.pipeline_client is None:
            return
        job_type = "alert_only" if source in {"external_media_alert", "ai_source_alert"} else "write_flow"
        self.pipeline_client.submit_job(
            job_type=job_type,
            task_id=task_id,
            source=source,
            source_item_id=source_item_id,
        )

    def _save_alert_page(self, source: NonMainstreamMediaSource, page: DiscoveredPage) -> None:
        if self._is_excluded(source, [page.title, page.excerpt]):
            self.repository.mark_seen(source, page.source_item_id, seeded=False)
            return
        task_id = self.repository.save_alert_task(source, page)
        if task_id is None:
            self.repository.mark_seen(source, page.source_item_id, seeded=False)
            return
        self._submit_pipeline_job(
            task_id=task_id,
            source=alert_only_task_source(source),
            source_item_id=page.source_item_id,
        )
        self.repository.mark_seen(source, page.source_item_id, seeded=False)

    def _save_article(self, source: NonMainstreamMediaSource, article: ParsedArticle) -> None:
        if self._is_excluded(source, [article.title, article.excerpt, article.content]):
            self.repository.mark_seen(source, article.canonical_url, seeded=False)
            return
        task_id = self.repository.save_task(source, article)
        if task_id is None:
            self.repository.mark_seen(source, article.canonical_url, seeded=False)
            return
        self._submit_pipeline_job(
            task_id=task_id,
            source=write_flow_task_source(source),
            source_item_id=article.canonical_url,
        )
        self.repository.mark_seen(source, article.canonical_url, seeded=False)

    def _is_excluded(self, source: NonMainstreamMediaSource, texts: list[str | None]) -> bool:
        if self.exclusion_matcher is None:
            return False
        return self.exclusion_matcher.is_excluded(
            scopes=media_source_exclusion_scopes(source.source_group),
            texts=texts,
        )

    def _attempt_fetch(
        self,
        *,
        source: NonMainstreamMediaSource,
        site_key: str,
        url: str,
        title: str | None = None,
    ) -> tuple[bool, str | None]:
        site = self.site_registry[site_key]
        if source.pipeline_mode == "alert_only":
            try:
                self._save_alert_page(source, self._build_page(url, title=title))
                self._clear_retry(site_key, url)
                return True, None
            except Exception as exc:
                return False, str(exc)
        try:
            article = self.fetch_article_fn(
                site,
                self._build_page(url, title=title),
                timeout_seconds=self.request_timeout_seconds,
                max_attempts=self.max_attempts,
                backoff_seconds=self.backoff_seconds,
            )
            self._save_article(source, article)
            self._clear_retry(site_key, url)
            return True, None
        except Exception as exc:
            return False, str(exc)

    def handle_message(
        self,
        message: Any,
        *,
        sources: dict[str, NonMainstreamMediaSource] | None = None,
    ) -> dict[str, Any]:
        active_sources = sources or self._telegram_sources()
        text = message_text(message).strip()
        site_key = extract_site_key(text)
        if site_key is None or site_key not in active_sources:
            return {"status": "skip"}
        url = extract_candidate_url(message, site_key)
        if not url:
            return {
                "status": "no_url",
                "site_key": site_key,
                "message_id": getattr(message, "id", None),
            }
        source = active_sources[site_key]
        title = extract_message_title(text, site_key, url)
        ok, error = self._attempt_fetch(source=source, site_key=site_key, url=url, title=title)
        if ok:
            return {
                "status": "success",
                "site_key": site_key,
                "url": url,
                "message_id": getattr(message, "id", None),
            }
        if should_retry_for_error_message(error):
            self._schedule_retry(
                source=source,
                site_key=site_key,
                url=url,
                message_id=int(getattr(message, "id", 0) or 0),
                retry_attempt=1,
            )
            return {
                "status": "retry_scheduled",
                "site_key": site_key,
                "url": url,
                "message_id": getattr(message, "id", None),
                "error": error,
                "retry_attempt": 1,
            }
        return {
            "status": "failed",
            "site_key": site_key,
            "url": url,
            "message_id": getattr(message, "id", None),
            "error": error,
        }

    def process_retry_tasks(self) -> list[dict[str, Any]]:
        now = self._now()
        due = sorted(
            (task for task in self._retry_tasks.values() if task.next_attempt_monotonic <= now),
            key=lambda item: (item.next_attempt_monotonic, item.site_key, item.url),
        )
        events: list[dict[str, Any]] = []
        for task in due:
            ok, error = self._attempt_fetch(source=task.source, site_key=task.site_key, url=task.url)
            if ok:
                events.append(
                    {
                        "status": "retry_success",
                        "site_key": task.site_key,
                        "url": task.url,
                        "message_id": task.message_id,
                        "retry_attempt": task.retry_attempt,
                    }
                )
                continue
            if task.retry_attempt >= self.settings.retry_max_attempts:
                self._clear_retry(task.site_key, task.url)
                events.append(
                    {
                        "status": "retry_exhausted",
                        "site_key": task.site_key,
                        "url": task.url,
                        "message_id": task.message_id,
                        "retry_attempt": task.retry_attempt,
                        "error": error,
                    }
                )
                continue
            if should_retry_for_error_message(error):
                next_attempt = task.retry_attempt + 1
                self._schedule_retry(
                    source=task.source,
                    site_key=task.site_key,
                    url=task.url,
                    message_id=task.message_id,
                    retry_attempt=next_attempt,
                )
                events.append(
                    {
                        "status": "retry_scheduled",
                        "site_key": task.site_key,
                        "url": task.url,
                        "message_id": task.message_id,
                        "retry_attempt": next_attempt,
                        "error": error,
                    }
                )
                continue
            self._clear_retry(task.site_key, task.url)
            events.append(
                {
                    "status": "failed",
                    "site_key": task.site_key,
                    "url": task.url,
                    "message_id": task.message_id,
                    "retry_attempt": task.retry_attempt,
                    "error": error,
                }
            )
        return events

    async def _ensure_login(self, client: Any) -> None:
        await client.connect()
        if await client.is_user_authorized():
            return
        await client.start()

    def _print_event(self, event: dict[str, Any]) -> None:
        status = event.get("status")
        if status in {None, "skip"}:
            return
        fields = [f"status={status}"]
        for key in ("site_key", "message_id", "retry_attempt", "url", "error"):
            value = event.get(key)
            if value is None:
                continue
            fields.append(f"{key}={value}")
        print("[odaily] telegram discovery " + " ".join(fields))

    async def run_forever(self) -> None:
        try:
            from telethon import TelegramClient
        except ModuleNotFoundError as exc:
            raise RuntimeError("telegram discovery requires telethon to be installed") from exc

        proxy = resolve_proxy(self.settings.proxy)
        client = TelegramClient(
            self.settings.session_path,
            self.settings.api_id,
            self.settings.api_hash,
            proxy=proxy,
            timeout=self.settings.timeout_seconds,
            connection_retries=self.settings.connection_retries,
            retry_delay=1,
        )
        try:
            await self._ensure_login(client)
            entity = await client.get_entity(self.channel_name)
            async for message in client.iter_messages(entity, limit=1):
                self._last_message_id = int(getattr(message, "id", 0) or 0)
                break
            self._maybe_sync_registry(force=True)
            print(
                "[odaily] telegram discovery worker started "
                f"channel={self.channel_name} last_message_id={self._last_message_id}"
            )
            while True:
                self._maybe_sync_registry()
                sources = self._telegram_sources()
                async for message in client.iter_messages(entity, min_id=self._last_message_id, reverse=True, limit=50):
                    self._last_message_id = max(self._last_message_id, int(getattr(message, "id", 0) or 0))
                    self._print_event(self.handle_message(message, sources=sources))
                for event in self.process_retry_tasks():
                    self._print_event(event)
                self._heartbeat.send(
                    status="ok",
                    success=True,
                    metadata={
                        "last_message_id": self._last_message_id,
                        "retry_queue_size": len(self._retry_tasks),
                        "sources": sorted(sources.keys()),
                    },
                )
                await asyncio.sleep(self.settings.poll_interval_seconds)
        except Exception as exc:
            self._heartbeat.send(
                status="failed",
                success=False,
                error=str(exc),
                metadata={
                    "last_message_id": self._last_message_id,
                    "retry_queue_size": len(self._retry_tasks),
                },
                force=True,
            )
            raise
        finally:
            await client.disconnect()
