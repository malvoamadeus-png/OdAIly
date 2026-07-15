from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

import requests
from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from packages.common.config import XProcessingSettings, load_x_processing_settings
from packages.common.console_auth import PostgresConsoleAuthRepository
from packages.common.editor_plugin_auth import (
    EditorPluginGenerationLogInput,
    PostgresEditorPluginAuthRepository,
)
from packages.common.paths import ensure_runtime_dirs, get_paths
from packages.common.text import normalize_multiline_text
from packages.x_capture.repository import PostgresXCaptureRepository
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient
from packages.x_processing.formatter import format_brief, parse_draft_output
from packages.x_processing.models import PROMPT_KEY_BY_NEWS_TYPE, PromptTemplateVersion, TaskRecord
from packages.x_processing.publisher_config import (
    PublisherRuleConfig,
    default_publisher_rule_config,
    load_publisher_rule_config,
    publisher_rule_config_payload,
    save_publisher_rule_config,
)
from packages.x_processing.repository import PostgresXProcessingRepository
from packages.x_processing.searcher import (
    AI_REVIEW_SCHEMA,
    CachedEmbeddingService,
    DashScopeEmbeddingClient,
    SearchDecision,
    SearchDocument,
    SearchMatch,
    SearchCache,
    build_ai_review_prompt,
    cosine_similarity,
    exact_duplicate_decision,
    parse_ai_review_output,
)
from packages.x_processing.worker import build_writer_prompt


QUICK_GENERATE_WRITER_MODEL = "gpt-5.4-mini"
QUICK_GENERATE_WRITER_REASONING_EFFORT = "low"
GENERATE_WRITER_REASONING_EFFORT = "low"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
PluginNewsType = Literal["regular", "funding", "onchain"]


class EditorPluginApiError(RuntimeError):
    status_code = HTTPStatus.BAD_REQUEST


class EditorPluginUnauthorizedError(EditorPluginApiError):
    status_code = HTTPStatus.UNAUTHORIZED


class EditorPluginForbiddenError(EditorPluginApiError):
    status_code = HTTPStatus.FORBIDDEN


class EditorPluginUpstreamError(EditorPluginApiError):
    status_code = HTTPStatus.BAD_GATEWAY


class EditorPluginRequestModel(BaseModel):
    source_type: str = "x_post"
    platform: str = "x"
    post_text: str
    post_url: HttpUrl | None = None
    post_id: str | None = None
    author_display_name: str | None = None
    author_handle: str | None = None
    posted_at: datetime | None = None
    news_type: PluginNewsType | None = None

    @field_validator("source_type")
    @classmethod
    def normalize_source_type(cls, value: str) -> str:
        text = value.strip().lower()
        if text != "x_post":
            raise ValueError("source_type must be x_post")
        return text

    @field_validator("platform")
    @classmethod
    def normalize_platform(cls, value: str) -> str:
        text = value.strip().lower()
        if text != "x":
            raise ValueError("platform must be x")
        return text

    @field_validator("post_text")
    @classmethod
    def normalize_post_text(cls, value: str) -> str:
        text = normalize_multiline_text(value)
        if not text:
            raise ValueError("post_text is required")
        return text

    @field_validator("post_id", "author_display_name", "author_handle")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class EditorPluginLoginRequestModel(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        text = value.strip().lower()
        if not text or "@" not in text:
            raise ValueError("email is required")
        return text

    @field_validator("password")
    @classmethod
    def require_password(cls, value: str) -> str:
        if not value:
            raise ValueError("password is required")
        return value


class EditorPluginApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    supabase_url: HttpUrl
    supabase_key: str
    auth_timeout_seconds: float = Field(default=15.0, gt=0.0, le=120.0)
    session_ttl_hours: float = Field(default=168.0, gt=0.0, le=720.0)
    generation_timeout_seconds: float = Field(default=120.0, gt=0.0, le=300.0)
    cors_allow_origin: str = "*"


@dataclass(frozen=True)
class AuthenticatedEditor:
    user_id: str | None
    email: str
    display_name: str | None


def load_editor_plugin_api_settings(*, host: str | None = None, port: int | None = None) -> EditorPluginApiSettings:
    load_dotenv()
    payload = {
        "host": host or os.getenv("EDITOR_PLUGIN_API_HOST") or "127.0.0.1",
        "port": port or int(os.getenv("EDITOR_PLUGIN_API_PORT") or 8765),
        "supabase_url": os.getenv("SUPABASE_URL") or os.getenv("VITE_SUPABASE_URL"),
        "supabase_key": os.getenv("SUPABASE_KEY") or os.getenv("VITE_SUPABASE_ANON_KEY"),
        "auth_timeout_seconds": float(os.getenv("EDITOR_PLUGIN_API_AUTH_TIMEOUT_SECONDS") or 15.0),
        "session_ttl_hours": float(os.getenv("EDITOR_PLUGIN_API_SESSION_TTL_HOURS") or 168.0),
        "generation_timeout_seconds": float(os.getenv("EDITOR_PLUGIN_API_GENERATION_TIMEOUT_SECONDS") or 120.0),
        "cors_allow_origin": os.getenv("EDITOR_PLUGIN_API_CORS_ALLOW_ORIGIN") or "*",
    }
    try:
        return EditorPluginApiSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid editor plugin API settings: {exc}") from exc


def parse_bearer_token(value: str | None) -> str:
    text = (value or "").strip()
    if not text.lower().startswith("bearer "):
        raise EditorPluginUnauthorizedError("登录状态已失效，请重新登录")
    token = text[7:].strip()
    if not token:
        raise EditorPluginUnauthorizedError("登录状态已失效，请重新登录")
    return token


def looks_like_openai_model(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith(("gpt-", "o"))


def is_deepseek_url(value: str) -> bool:
    return "deepseek" in value.lower()


def hash_plugin_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def format_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    if not details:
        return "请求参数不合法"
    first = details[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "__root__")
    message = str(first.get("msg") or "参数不合法")
    if location:
        return f"{location}: {message}"
    return message


class SupabaseEditorPluginAuthenticator:
    def __init__(
        self,
        *,
        settings: EditorPluginApiSettings,
        repository: PostgresEditorPluginAuthRepository,
    ) -> None:
        self.settings = settings
        self.repository = repository

    def login(self, request: EditorPluginLoginRequestModel) -> tuple[str, datetime, AuthenticatedEditor]:
        try:
            user_id, email = self.repository.verify_supabase_password(request.email, request.password)
        except ValueError as exc:
            raise EditorPluginUnauthorizedError("邮箱或密码错误") from exc
        record = self.repository.get_enabled_user(email)
        if record is None:
            raise EditorPluginForbiddenError("当前账号未加入插件白名单或已被停用")
        display_name = record.display_name or email.split("@", 1)[0]
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(hours=self.settings.session_ttl_hours)
        self.repository.create_session(
            token_hash=hash_plugin_token(token),
            user_id=user_id,
            email=record.email,
            display_name=display_name,
            expires_at=expires_at,
        )
        return token, expires_at, AuthenticatedEditor(user_id=user_id, email=record.email, display_name=display_name)

    def logout(self, authorization_header: str | None) -> None:
        token = parse_bearer_token(authorization_header)
        self.repository.delete_session(hash_plugin_token(token))

    def authenticate(self, authorization_header: str | None) -> AuthenticatedEditor:
        token = parse_bearer_token(authorization_header)
        session = self.repository.get_session(hash_plugin_token(token))
        if session is not None:
            record = self.repository.get_enabled_user(session.email)
            if record is None:
                raise EditorPluginForbiddenError("当前账号未加入插件白名单或已被停用")
            return AuthenticatedEditor(
                user_id=session.user_id,
                email=record.email,
                display_name=record.display_name or session.display_name or record.email.split("@", 1)[0],
            )

        try:
            response = requests.get(
                f"{str(self.settings.supabase_url).rstrip('/')}/auth/v1/user",
                headers={
                    "apikey": self.settings.supabase_key,
                    "Authorization": f"Bearer {token}",
                },
                timeout=self.settings.auth_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试") from exc

        if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise EditorPluginUnauthorizedError("登录状态已失效，请重新登录")
        if not response.ok:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试")

        try:
            payload = response.json()
        except ValueError as exc:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试") from exc
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise EditorPluginUnauthorizedError("当前登录态缺少邮箱信息")

        record = self.repository.get_enabled_user(email)
        if record is None:
            raise EditorPluginForbiddenError("当前账号未加入插件白名单或已被停用")

        metadata = payload.get("user_metadata") if isinstance(payload.get("user_metadata"), dict) else {}
        display_name = (
            record.display_name
            or str(metadata.get("display_name") or "").strip()
            or str(metadata.get("name") or "").strip()
            or email.split("@", 1)[0]
        )
        user_id = str(payload.get("id") or "").strip() or None
        return AuthenticatedEditor(user_id=user_id, email=record.email, display_name=display_name)


class EditorPluginNewsGenService:
    def __init__(
        self,
        *,
        database_url: str | None,
        api_settings: EditorPluginApiSettings,
        x_settings: XProcessingSettings,
    ) -> None:
        self.api_settings = api_settings
        self.x_settings = x_settings
        self.paths = get_paths()
        ensure_runtime_dirs(self.paths)

        self.auth_repository = PostgresEditorPluginAuthRepository(database_url)
        self.console_auth_repository = PostgresConsoleAuthRepository(database_url)
        self.x_capture_repository = PostgresXCaptureRepository(database_url)
        self.x_repository = PostgresXProcessingRepository(database_url)

        self.authenticator = SupabaseEditorPluginAuthenticator(
            settings=api_settings,
            repository=self.auth_repository,
        )

        self.search_ai_client = self._build_search_ai_client()
        self.embedding_service = self._build_embedding_service()

    def _build_search_ai_client(self) -> TextGenerationClient:
        api_key = self.x_settings.search_ai_review_openai_api_key or self.x_settings.openai_api_key
        if not api_key:
            raise RuntimeError("Missing search AI review OpenAI API key")
        return OpenAIResponsesClient(
            api_key=api_key,
            base_url=str(self.x_settings.search_ai_review_openai_base_url or self.x_settings.openai_base_url),
            api_style=self.x_settings.search_ai_review_openai_api_style or self.x_settings.openai_api_style,
            timeout_seconds=self.api_settings.generation_timeout_seconds,
            max_attempts=self.x_settings.retry.max_attempts,
            backoff_seconds=self.x_settings.retry.backoff_seconds,
            omit_reasoning_effort=self.x_settings.search_ai_review_omit_reasoning_effort,
            chat_response_format_mode=self.x_settings.search_ai_review_chat_response_format_mode,
            append_json_schema_to_prompt=self.x_settings.search_ai_review_append_json_schema_to_prompt,
        )

    def _build_writer_ai_client(self, *, model: str) -> TextGenerationClient:
        api_key = os.getenv("EDITOR_PLUGIN_WRITER_OPENAI_API_KEY") or self.x_settings.openai_api_key
        if not api_key:
            raise RuntimeError("Missing writer OpenAI API key")
        base_url = os.getenv("EDITOR_PLUGIN_WRITER_OPENAI_BASE_URL") or str(self.x_settings.openai_base_url)
        api_style = os.getenv("EDITOR_PLUGIN_WRITER_OPENAI_API_STYLE") or self.x_settings.openai_api_style
        if looks_like_openai_model(model) and is_deepseek_url(base_url) and not os.getenv(
            "EDITOR_PLUGIN_WRITER_OPENAI_BASE_URL"
        ):
            base_url = DEFAULT_OPENAI_BASE_URL
            if not os.getenv("EDITOR_PLUGIN_WRITER_OPENAI_API_STYLE"):
                api_style = "responses"
        if api_style not in {"responses", "chat_completions"}:
            raise RuntimeError("Invalid editor plugin writer OpenAI API style")
        return OpenAIResponsesClient(
            api_key=api_key,
            base_url=base_url,
            api_style=api_style,
            timeout_seconds=self.api_settings.generation_timeout_seconds,
            max_attempts=self.x_settings.retry.max_attempts,
            backoff_seconds=self.x_settings.retry.backoff_seconds,
        )

    def _build_embedding_service(self) -> CachedEmbeddingService:
        if not self.x_settings.dashscope_api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY")
        return CachedEmbeddingService(
            client=DashScopeEmbeddingClient(
                api_key=self.x_settings.dashscope_api_key,
                base_url=str(self.x_settings.search_embedding_base_url),
                model=self.x_settings.search_embedding_model,
                timeout_seconds=self.x_settings.request_timeout_seconds,
                max_attempts=self.x_settings.retry.max_attempts,
                backoff_seconds=self.x_settings.retry.backoff_seconds,
            ),
            cache=SearchCache(self.paths.searcher_cache_path),
        )

    def authenticate(self, authorization_header: str | None) -> AuthenticatedEditor:
        return self.authenticator.authenticate(authorization_header)

    def authenticate_console_admin(self, authorization_header: str | None) -> AuthenticatedEditor:
        token = parse_bearer_token(authorization_header)
        try:
            response = requests.get(
                f"{str(self.api_settings.supabase_url).rstrip('/')}/auth/v1/user",
                headers={
                    "apikey": self.api_settings.supabase_key,
                    "Authorization": f"Bearer {token}",
                },
                timeout=self.api_settings.auth_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试") from exc

        if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise EditorPluginUnauthorizedError("登录状态已失效，请重新登录")
        if not response.ok:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试")

        try:
            payload = response.json()
        except ValueError as exc:
            raise EditorPluginUpstreamError("Supabase 用户校验失败，请稍后再试") from exc
        email = str(payload.get("email") or "").strip().lower()
        if not email:
            raise EditorPluginUnauthorizedError("当前登录态缺少邮箱信息")
        record = self.console_auth_repository.get_admin(email)
        if record is None:
            raise EditorPluginForbiddenError("当前账号未加入控制台管理员白名单")
        metadata = payload.get("user_metadata") if isinstance(payload.get("user_metadata"), dict) else {}
        display_name = str(metadata.get("display_name") or "").strip() or str(metadata.get("name") or "").strip()
        return AuthenticatedEditor(
            user_id=str(payload.get("id") or "").strip() or None,
            email=record.email,
            display_name=display_name or record.email.split("@", 1)[0],
        )

    def login(self, request: EditorPluginLoginRequestModel) -> dict[str, Any]:
        token, expires_at, actor = self.authenticator.login(request)
        return {
            "access_token": token,
            "expires_at": int(expires_at.timestamp()),
            "user": {
                "id": actor.user_id,
                "email": actor.email,
                "display_name": actor.display_name,
            },
        }

    def logout(self, authorization_header: str | None) -> None:
        self.authenticator.logout(authorization_header)

    def profile(self, actor: AuthenticatedEditor) -> dict[str, Any]:
        return {
            "email": actor.email,
            "display_name": actor.display_name or actor.email.split("@", 1)[0],
            "enabled": True,
        }

    def get_publisher_rules(self, actor: AuthenticatedEditor) -> dict[str, Any]:
        config = load_publisher_rule_config()
        if config.updated_at is None:
            try:
                snapshot = self.x_repository.get_publisher_rule_config_snapshot()
            except Exception as exc:
                print(f"[odaily] publisher rules snapshot load skipped error={exc}")
                snapshot = None
            if snapshot:
                try:
                    config = PublisherRuleConfig.model_validate(snapshot)
                    config = save_publisher_rule_config(config, updated_by=config.updated_by)
                except ValidationError:
                    config = default_publisher_rule_config()
        return publisher_rule_config_payload(config)

    def save_publisher_rules(self, actor: AuthenticatedEditor, payload: dict[str, Any]) -> dict[str, Any]:
        raw_config = payload.get("config")
        if not isinstance(raw_config, dict):
            raise EditorPluginApiError("config 必须是 JSON 对象")
        config = PublisherRuleConfig.model_validate(raw_config)
        saved_config = save_publisher_rule_config(config, updated_by=actor.email)
        snapshot = publisher_rule_config_payload(saved_config)
        try:
            self.x_repository.upsert_publisher_rule_config_snapshot(
                config_json=snapshot["config"],
                prompt_text=str(snapshot["prompt_text"]),
                updated_by=actor.email,
            )
        except Exception as exc:
            print(f"[odaily] publisher rules snapshot save skipped error={exc}")
        return snapshot

    def feed(self, actor: AuthenticatedEditor, limit: int = 120) -> list[dict[str, Any]]:
        rows = self.auth_repository.call_plugin_function(
            email=actor.email,
            function_name="editor_plugin_feed",
            args=(limit,),
        )
        return [self._normalize_feed_row(row) for row in rows]

    def feed_state(self, actor: AuthenticatedEditor, feed_item_ids: list[str]) -> list[dict[str, Any]]:
        return self.auth_repository.call_plugin_function(
            email=actor.email,
            function_name="editor_plugin_state",
            args=(feed_item_ids,),
        )

    def mark_seen(self, actor: AuthenticatedEditor, payload: dict[str, Any]) -> Any:
        return self.auth_repository.call_plugin_json_function(
            email=actor.email,
            function_name="editor_plugin_mark_seen",
            args=(
                str(payload.get("p_feed_item_id") or ""),
                str(payload.get("p_feed_kind") or ""),
                payload.get("p_session_id"),
                json.dumps(payload.get("p_extra_json") or {}, ensure_ascii=False),
            ),
        )

    def submit_feedback(self, actor: AuthenticatedEditor, payload: dict[str, Any]) -> Any:
        return self.auth_repository.call_plugin_json_function(
            email=actor.email,
            function_name="editor_plugin_submit_feedback",
            args=(
                str(payload.get("p_feed_item_id") or ""),
                str(payload.get("p_feed_kind") or ""),
                str(payload.get("p_feedback") or ""),
                payload.get("p_session_id"),
                json.dumps(payload.get("p_extra_json") or {}, ensure_ascii=False),
            ),
        )

    def _normalize_feed_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "badges": row.get("badges") or [],
            "action_schema": row.get("action_schema") or {"type": "read"},
            "meta_json": row.get("meta_json") or {},
        }

    def run_search(self, actor: AuthenticatedEditor, request: EditorPluginRequestModel) -> dict[str, Any]:
        query = self._build_query_document(request)
        result_payload: dict[str, Any] | None = None
        try:
            since = datetime.now(UTC) - timedelta(hours=self.x_settings.search_window_hours)
            odaily_documents = self.x_repository.list_odaily_reference_documents(since=since)
            active_documents = self.x_repository.list_active_candidate_documents()

            decision = exact_duplicate_decision(
                query=query,
                documents=odaily_documents,
                target_type="odaily_published",
            )
            if decision is None:
                decision = exact_duplicate_decision(
                    query=query,
                    documents=active_documents,
                    target_type="inflight_candidate",
                )

            query_vector = self.embedding_service.embed_one(
                cache_key=f"editor-plugin-query:{query.doc_id}",
                text=query.embedding_text,
            )
            vector_candidates = self._build_vector_candidates(
                query_vector=query_vector,
                odaily_documents=odaily_documents,
                active_documents=active_documents,
            )

            if decision is None:
                odaily_match = self._top_vector_match(query_vector=query_vector, documents=odaily_documents)
                decision = self._decide_vector_match(query=query, match=odaily_match, target_type="odaily_published")
            if decision is None:
                active_match = self._top_vector_match(query_vector=query_vector, documents=active_documents)
                decision = self._decide_vector_match(query=query, match=active_match, target_type="inflight_candidate")

            result_payload = {
                "kind": "search",
                "is_duplicate": bool(decision and decision.is_duplicate),
                "reason": decision.reason if decision else "no_match",
                "summary": self._search_summary(decision),
                "top_candidates": vector_candidates,
            }
            self._log_request(
                actor=actor,
                request=request,
                action="search",
                status="success",
                route=None,
                result_json=result_payload,
                error_message=None,
            )
            return result_payload
        except Exception as exc:
            self._log_request(
                actor=actor,
                request=request,
                action="search",
                status="failed",
                route=None,
                result_json=result_payload or {},
                error_message=str(exc),
            )
            raise

    def run_generate(self, actor: AuthenticatedEditor, request: EditorPluginRequestModel) -> dict[str, Any]:
        return self._run_generate(
            actor,
            request,
            action="generate",
            writer_model=self.x_settings.writer_model,
            writer_reasoning_effort=GENERATE_WRITER_REASONING_EFFORT,
        )

    def run_quick_generate(self, actor: AuthenticatedEditor, request: EditorPluginRequestModel) -> dict[str, Any]:
        return self._run_generate(
            actor,
            request,
            action="quick_generate",
            writer_model=QUICK_GENERATE_WRITER_MODEL,
            writer_reasoning_effort=QUICK_GENERATE_WRITER_REASONING_EFFORT,
        )

    def _run_generate(
        self,
        actor: AuthenticatedEditor,
        request: EditorPluginRequestModel,
        *,
        action: str,
        writer_model: str,
        writer_reasoning_effort: str,
    ) -> dict[str, Any]:
        task = self._build_task_record(request)
        route: str | None = None
        result_payload: dict[str, Any] | None = None
        try:
            route = self._require_news_type(request)
            prompt = self._get_prompt(PROMPT_KEY_BY_NEWS_TYPE[route])
            writer_prompt = build_writer_prompt(task=task, prompt=prompt)
            raw_output = self._build_writer_ai_client(model=writer_model).generate_text(
                model=writer_model,
                prompt=writer_prompt,
                reasoning_effort=writer_reasoning_effort,
            )
            final = format_brief(parse_draft_output(raw_output))
            result_payload = {
                "kind": "generate",
                "route": route,
                "title": final.title,
                "content": final.content,
                "raw_source_text": request.post_text,
            }
            self._log_request(
                actor=actor,
                request=request,
                action=action,
                status="success",
                route=route,
                result_json=result_payload,
                error_message=None,
            )
            return result_payload
        except Exception as exc:
            self._log_request(
                actor=actor,
                request=request,
                action=action,
                status="failed",
                route=route,
                result_json=result_payload or {},
                error_message=str(exc),
            )
            raise

    def _build_query_document(self, request: EditorPluginRequestModel) -> SearchDocument:
        doc_id = request.post_id or hashlib.sha1(request.post_text.encode("utf-8")).hexdigest()
        return SearchDocument(
            doc_type="editor_plugin_query",
            doc_id=doc_id,
            title=None,
            content=request.post_text,
            source="x",
            source_url=str(request.post_url) if request.post_url else None,
            published_at=request.posted_at,
            metadata={
                "author_display_name": request.author_display_name,
                "author_handle": request.author_handle,
            },
        )

    def _build_task_record(self, request: EditorPluginRequestModel) -> TaskRecord:
        source_item_id = request.post_id or hashlib.sha1(request.post_text.encode("utf-8")).hexdigest()[:16]
        effective_author_name = self.x_capture_repository.resolve_effective_author_name(
            author_username=request.author_handle,
            author_display_name=request.author_display_name,
        )
        return TaskRecord(
            id=0,
            source="x",
            source_item_id=source_item_id,
            source_url=str(request.post_url) if request.post_url else None,
            title=None,
            content=request.post_text,
            published_at=request.posted_at,
            metadata={
                "author_display_name": request.author_display_name,
                "author_username": request.author_handle,
                "effective_author_name": effective_author_name,
            },
        )

    def _require_news_type(self, request: EditorPluginRequestModel) -> PluginNewsType:
        if request.news_type is None:
            raise EditorPluginApiError("请选择生成类型：常规、融资或链上")
        return request.news_type

    def _get_prompt(self, template_key: str) -> PromptTemplateVersion:
        return self.x_repository.get_active_prompt(template_key)

    def _top_vector_match(
        self,
        *,
        query_vector: list[float],
        documents: list[SearchDocument],
    ) -> SearchMatch | None:
        if not documents:
            return None
        embeddings = self.embedding_service.embed_documents(documents)
        best: SearchMatch | None = None
        for document, vector in embeddings:
            similarity = cosine_similarity(query_vector, vector)
            if best is None or similarity > best.similarity:
                best = SearchMatch(document=document, similarity=similarity)
        return best

    def _build_vector_candidates(
        self,
        *,
        query_vector: list[float],
        odaily_documents: list[SearchDocument],
        active_documents: list[SearchDocument],
    ) -> list[dict[str, Any]]:
        scored: list[tuple[SearchDocument, float, str]] = []
        for document, vector in self.embedding_service.embed_documents(odaily_documents):
            scored.append((document, cosine_similarity(query_vector, vector), "odaily_published"))
        for document, vector in self.embedding_service.embed_documents(active_documents):
            scored.append((document, cosine_similarity(query_vector, vector), "inflight_candidate"))

        seen: set[tuple[str, str]] = set()
        rows: list[dict[str, Any]] = []
        for document, similarity, target_type in sorted(scored, key=lambda item: item[1], reverse=True):
            key = (target_type, document.doc_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "target_type": target_type,
                    "target_id": document.doc_id,
                    "title": document.title or "无标题",
                    "published_at": document.published_at.isoformat() if document.published_at else None,
                    "source_url": document.source_url,
                    "similarity": round(similarity, 4),
                }
            )
            if len(rows) >= 5:
                break
        return rows

    def _decide_vector_match(
        self,
        *,
        query: SearchDocument,
        match: SearchMatch | None,
        target_type: str,
    ) -> SearchDecision | None:
        if match is None:
            return None
        candidate_id = match.document.candidate_id if target_type == "inflight_candidate" else None
        if match.similarity >= self.x_settings.search_duplicate_threshold:
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=match.document.doc_id,
                reason="same_event",
                similarity=match.similarity,
                candidate_id=candidate_id,
            )
        if match.similarity < self.x_settings.search_ai_review_threshold:
            return None
        raw_output = self.search_ai_client.generate_text(
            model=self.x_settings.search_ai_review_model,
            prompt=build_ai_review_prompt(query=query, match=match),
            text_format=AI_REVIEW_SCHEMA,
            reasoning_effort=self.x_settings.search_ai_review_reasoning_effort,
        )
        payload = parse_ai_review_output(raw_output)
        is_duplicate = bool(payload.get("is_duplicate"))
        duplicate_type = str(payload.get("duplicate_target_type") or "none")
        return SearchDecision(
            is_duplicate=is_duplicate,
            duplicate_target_type=duplicate_type if is_duplicate else "none",
            duplicate_target_id=str((payload.get("duplicate_target_id") or match.document.doc_id) if is_duplicate else ""),
            reason=str(payload.get("reason") or "unrelated"),
            similarity=match.similarity,
            candidate_id=candidate_id if is_duplicate and duplicate_type == "inflight_candidate" else None,
            raw_ai_output=raw_output,
        )

    def _search_summary(self, decision: SearchDecision | None) -> str:
        if decision is None:
            return "未发现明显重复"
        if decision.is_duplicate:
            return "疑似重复，建议先对照历史快讯"
        if decision.reason == "update_of_existing_event":
            return "更像同一事件的新进展，可人工判断是否继续生成"
        return "未发现明显重复"

    def _log_request(
        self,
        *,
        actor: AuthenticatedEditor,
        request: EditorPluginRequestModel,
        action: str,
        status: str,
        route: str | None,
        result_json: dict[str, Any],
        error_message: str | None,
    ) -> None:
        try:
            self.auth_repository.insert_generation_log(
                EditorPluginGenerationLogInput(
                    action=action,
                    actor_user_id=actor.user_id,
                    actor_email=actor.email,
                    actor_display_name=actor.display_name,
                    source_type=request.source_type,
                    platform=request.platform,
                    post_id=request.post_id,
                    post_url=str(request.post_url) if request.post_url else None,
                    author_display_name=request.author_display_name,
                    author_handle=request.author_handle,
                    posted_at=request.posted_at.isoformat() if request.posted_at else None,
                    request_text=request.post_text,
                    route=route,
                    result_json=result_json,
                    status=status,
                    error_message=error_message,
                )
            )
        except Exception:
            # Logging must not break the primary request flow.
            return


class EditorPluginApiServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], service: EditorPluginNewsGenService) -> None:
        super().__init__(server_address, EditorPluginApiHandler)
        self.service = service


class EditorPluginApiHandler(BaseHTTPRequestHandler):
    server_version = "OdAIlyEditorPluginAPI/0.1"
    AUTH_PATHS = {"/plugin/auth/login", "/plugin/auth/logout", "/plugin/auth/profile"}
    FEED_PATHS = {"/plugin/feed/items", "/plugin/feed/state", "/plugin/feed/mark-seen", "/plugin/feed/feedback"}
    NEWS_GEN_PATHS = {"/plugin/news-gen/search", "/plugin/news-gen/generate", "/plugin/news-gen/quick-generate"}
    CONSOLE_PATHS = {"/console/publisher-rules/get", "/console/publisher-rules/save"}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in self.AUTH_PATHS | self.FEED_PATHS | self.NEWS_GEN_PATHS | self.CONSOLE_PATHS:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "Not found"})
            return

        try:
            if self.path == "/plugin/auth/login":
                request = EditorPluginLoginRequestModel.model_validate(self._read_json())
                data = self.server.service.login(request)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
                return

            if self.path in self.CONSOLE_PATHS:
                actor = self.server.service.authenticate_console_admin(self.headers.get("Authorization"))
                if self.path == "/console/publisher-rules/get":
                    self._send_json(HTTPStatus.OK, {"ok": True, "data": self.server.service.get_publisher_rules(actor)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "data": self.server.service.save_publisher_rules(actor, self._read_json())},
                )
                return

            actor = self.server.service.authenticate(self.headers.get("Authorization"))
            if self.path == "/plugin/auth/logout":
                self.server.service.logout(self.headers.get("Authorization"))
                self._send_json(HTTPStatus.OK, {"ok": True, "data": None})
                return
            if self.path == "/plugin/auth/profile":
                self._send_json(HTTPStatus.OK, {"ok": True, "data": self.server.service.profile(actor)})
                return
            if self.path == "/plugin/feed/items":
                payload = self._read_json()
                limit = int(payload.get("limit") or 120)
                self._send_json(HTTPStatus.OK, {"ok": True, "data": self.server.service.feed(actor, limit)})
                return
            if self.path == "/plugin/feed/state":
                payload = self._read_json()
                feed_item_ids = payload.get("feed_item_ids") or []
                if not isinstance(feed_item_ids, list):
                    raise EditorPluginApiError("feed_item_ids must be an array")
                self._send_json(HTTPStatus.OK, {"ok": True, "data": self.server.service.feed_state(actor, feed_item_ids)})
                return
            if self.path == "/plugin/feed/mark-seen":
                self._send_json(HTTPStatus.OK, {"ok": True, "data": self.server.service.mark_seen(actor, self._read_json())})
                return
            if self.path == "/plugin/feed/feedback":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "data": self.server.service.submit_feedback(actor, self._read_json())},
                )
                return

            payload = self._read_json()
            request = EditorPluginRequestModel.model_validate(payload)
            if self.path == "/plugin/news-gen/search":
                data = self.server.service.run_search(actor, request)
            elif self.path == "/plugin/news-gen/quick-generate":
                data = self.server.service.run_quick_generate(actor, request)
            else:
                data = self.server.service.run_generate(actor, request)
            self._send_json(HTTPStatus.OK, {"ok": True, "data": data})
        except EditorPluginApiError as exc:
            self._send_json(exc.status_code, {"ok": False, "message": str(exc)})
        except ValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": format_validation_error(exc)})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "请求体不是有效 JSON"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print("[odaily] editor-plugin-api " + format % args)

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length") or "0")
        if content_length < 1:
            raise EditorPluginApiError("请求体不能为空")
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise EditorPluginApiError("请求体必须是 JSON 对象")
        return payload

    def _send_common_headers(self) -> None:
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", self.server.service.api_settings.cors_allow_origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=self._json_default).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_default(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)


def run_editor_plugin_api_server(
    *,
    database_url: str | None,
    host: str | None = None,
    port: int | None = None,
) -> int:
    api_settings = load_editor_plugin_api_settings(host=host, port=port)
    service = EditorPluginNewsGenService(
        database_url=database_url,
        api_settings=api_settings,
        x_settings=load_x_processing_settings(),
    )
    server = EditorPluginApiServer((api_settings.host, api_settings.port), service)
    print(
        "[odaily] editor plugin api server started "
        f"host={api_settings.host} port={api_settings.port} "
        f"generation_timeout_seconds={api_settings.generation_timeout_seconds:g}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[odaily] editor plugin api server stopped")
    finally:
        server.server_close()
    return 0
