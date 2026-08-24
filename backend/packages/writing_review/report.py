from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.config import load_auditor_settings
from packages.common.paths import get_paths
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient


WRITING_REVIEW_PROMPT_VERSION = "writing_review_zh_v1"


class ReviewClient(Protocol):
    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str: ...


WRITING_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "name": "odaily_writing_review",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["no_change", "minor_edit", "rewrite"]},
            "summary": {"type": "string"},
            "title_suggestion": {"type": "string"},
            "content_suggestion": {"type": "string"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "location": {"type": "string", "enum": ["title", "content"]},
                        "type": {
                            "type": "string",
                            "enum": [
                                "focus",
                                "redundancy",
                                "wording",
                                "structure",
                                "logic",
                                "translation",
                                "format",
                                "other",
                            ],
                        },
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "original": {"type": "string"},
                        "suggested": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["location", "type", "severity", "original", "suggested", "reason"],
                },
            },
            "patterns": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["verdict", "summary", "title_suggestion", "content_suggestion", "issues", "patterns"],
    },
    "strict": True,
}


@dataclass(frozen=True, slots=True)
class WritingReviewItem:
    task_id: int
    source: str
    source_item_id: str
    source_url: str | None
    created_at: str | None
    write_completed_at: str | None
    writer_model: str | None
    original_title: str
    original_content: str
    title: str
    content: str
    is_final: bool


@dataclass(frozen=True, slots=True)
class WritingReviewResult:
    item: WritingReviewItem
    model: str
    raw_output: str | None
    verdict: str | None
    summary: str
    title_suggestion: str
    content_suggestion: str
    issues: list[dict[str, str]] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WritingReviewReport:
    generated_at: str
    since: str
    until: str
    lookback_hours: float
    model: str
    prompt_version: str
    items: list[WritingReviewResult]

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(item.verdict or "error" for item in self.items)
        return {key: counts[key] for key in ("no_change", "minor_edit", "rewrite", "error") if counts[key]}

    @property
    def issue_counts(self) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for item in self.items:
            for issue in item.issues:
                counts[issue.get("type") or "other"] += 1
            for pattern in item.patterns:
                counts[f"pattern:{pattern}"] += 1
        return dict(counts.most_common())


def load_ai_written_items(
    database_path: Path,
    *,
    since: datetime,
    until: datetime,
    limit: int | None = None,
) -> list[WritingReviewItem]:
    """Read completed writer outputs without changing the production database."""
    if not database_path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {database_path}")
    query = """
        SELECT
            t.id AS task_id, t.source, t.source_item_id, t.source_url, t.created_at,
            t.title AS original_title, t.content AS original_content,
            p.write_completed_at, p.writer_model,
            p.draft_title, p.draft_content, p.final_title, p.final_content
        FROM tasks t
        JOIN x_task_pipeline p ON p.task_id = t.id
        WHERE p.write_completed_at IS NOT NULL
          AND p.writer_model IS NOT NULL
          AND trim(p.writer_model) <> ''
          AND julianday(p.write_completed_at) >= julianday(?)
          AND julianday(p.write_completed_at) <= julianday(?)
          AND trim(COALESCE(p.final_content, p.draft_content, '')) <> ''
        ORDER BY julianday(p.write_completed_at) DESC, t.id DESC
    """
    parameters: list[Any] = [since.astimezone(UTC).isoformat(), until.astimezone(UTC).isoformat()]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        query += " LIMIT ?"
        parameters.append(limit)
    uri = f"file:{database_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot open SQLite database read-only: {database_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, parameters).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read AI-written pipeline rows: {exc}") from exc
    finally:
        conn.close()

    result: list[WritingReviewItem] = []
    for row in rows:
        final_title = _clean_text(row["final_title"])
        final_content = _clean_text(row["final_content"])
        draft_title = _clean_text(row["draft_title"])
        draft_content = _clean_text(row["draft_content"])
        is_final = bool(final_content)
        result.append(
            WritingReviewItem(
                task_id=int(row["task_id"]),
                source=str(row["source"] or ""),
                source_item_id=str(row["source_item_id"] or ""),
                source_url=row["source_url"],
                created_at=row["created_at"],
                write_completed_at=row["write_completed_at"],
                writer_model=row["writer_model"],
                original_title=_clean_text(row["original_title"]),
                original_content=_clean_text(row["original_content"]),
                title=final_title or draft_title,
                content=final_content or draft_content,
                is_final=is_final,
            )
        )
    return result


def build_writing_review_prompt(item: WritingReviewItem) -> str:
    return f"""你是 Odaily 的中文快讯编辑，只审核 AI 已经写出的标题和正文是否还能在写法上改进。

你的任务是编辑批评和改写，不是事实核查，也不是发布决策。请只关注：信息重点、标题取舍、表达顺序、简洁度、中文自然度、快讯感，以及标题与正文之间的内部逻辑。原始输入只作为理解上下文，不能据此臆造任何新事实。

重点检查：
- 标题是否结果/动作前置，是否被过多背景、身份或不重要的人名分散；
- 同一主体做多个动作时是否重复出现主体，是否有名词或句意重复；
- 标题是否过长、拗口、像外媒直译，或使用不符合快讯语感的词；
- 标题与正文是否出现“清仓后又亏损”等明确的时间、动作或因果关系冲突；
- 正文是否围绕一个核心事实，是否东一句西一句、堆砌背景或重复标题；
- 背景信息是否应改成短定语，数字是否可以在不损失信息的前提下简洁表达；
- 只在确实有明显收益时提出修改。没有必要修改时输出 no_change，不要为了显示意见而润色。

可参考的编辑偏好：不重要人物的个人观点通常弱化姓名，可用“分析：观点”承接；重要动作优先于机构规模等背景；同一标题中的中英文名称不要无意义重复；正文应像一条快讯，不要写成综述或评论。

不要检查：事实真伪、来源是否可靠、是否应该发布、竞品/营销/政治等分类、流水线是否调用了某个 worker、标点风格、与外部知识相关的实体或数字纠错。不要因为个人偏好强行重写。建议必须保留原文事实，不得添加原文或上下文没有提供的内容。

输出要求：
- verdict 为 no_change / minor_edit / rewrite；
- issues 只列有明确收益的问题，original 必须是当前标题或正文中的原文片段；
- title_suggestion 和 content_suggestion 必须是完整的可直接替换文本；无修改时原样返回；
- summary 用一句中文说明判断；patterns 只写可复用的问题模式名称，最多 3 个；
- 只输出 JSON，不要输出 Markdown、解释过程或额外字段。

【来源】{item.source}
【原始标题】{item.original_title}
【原始正文】{item.original_content}
【AI稿件版本】{"最终稿" if item.is_final else "草稿"}
【AI标题】{item.title}
【AI正文】{item.content}
"""


def review_item(
    item: WritingReviewItem,
    *,
    client: ReviewClient,
    model: str,
    reasoning_effort: str | None,
) -> WritingReviewResult:
    try:
        raw_output = client.generate_text(
            model=model,
            prompt=build_writing_review_prompt(item),
            text_format=WRITING_REVIEW_SCHEMA,
            reasoning_effort=reasoning_effort,
        )
        payload = _parse_payload(raw_output)
        return _normalize_result(item, model=model, raw_output=raw_output, payload=payload)
    except Exception as exc:
        return WritingReviewResult(
            item=item,
            model=model,
            raw_output=None,
            verdict=None,
            summary="",
            title_suggestion=item.title,
            content_suggestion=item.content,
            error=str(exc),
        )


def generate_writing_review_report(
    items: list[WritingReviewItem],
    *,
    client: ReviewClient,
    model: str,
    since: datetime,
    until: datetime,
    lookback_hours: float,
    reasoning_effort: str | None,
) -> WritingReviewReport:
    results = [
        review_item(item, client=client, model=model, reasoning_effort=reasoning_effort)
        for item in items
    ]
    return WritingReviewReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        since=since.astimezone(UTC).isoformat(timespec="seconds"),
        until=until.astimezone(UTC).isoformat(timespec="seconds"),
        lookback_hours=lookback_hours,
        model=model,
        prompt_version=WRITING_REVIEW_PROMPT_VERSION,
        items=results,
    )


def build_writing_review_client(settings: Any | None = None) -> tuple[OpenAIResponsesClient, str, str | None]:
    settings = settings or load_auditor_settings()
    api_key = settings.openai_api_key
    if not api_key:
        raise ValueError("writing review needs an API key; configure AUDITOR_OPENAI_API_KEY or the shared LLM key")
    model = os.getenv("WRITING_REVIEW_MODEL") or settings.model
    return (
        OpenAIResponsesClient(
            api_key=api_key,
            base_url=str(settings.openai_base_url),
            api_style=settings.openai_api_style,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
            omit_reasoning_effort=settings.omit_reasoning_effort,
            chat_response_format_mode=settings.chat_response_format_mode,
            append_json_schema_to_prompt=settings.append_json_schema_to_prompt,
        ),
        model,
        settings.reasoning_effort,
    )


def write_report_files(report: WritingReviewReport, output_dir: Path, *, stamp: str | None = None) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = stamp or datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    json_path = output_dir / f"writing-review-{filename}.json"
    markdown_path = output_dir / f"writing-review-{filename}.md"
    payload = asdict(report)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return markdown_path, json_path


def render_markdown_report(report: WritingReviewReport) -> str:
    lines = [
        "# AI 快讯写作审阅报告",
        "",
        f"- 审阅窗口：{report.since} 至 {report.until}（UTC）",
        f"- 生成时间：{report.generated_at}（UTC）",
        f"- 模型：`{report.model}`",
        f"- Prompt：`{report.prompt_version}`",
        f"- 样本数：{len(report.items)}",
        "",
        "## 汇总",
        "",
    ]
    labels = {"no_change": "无需修改", "minor_edit": "轻微修改", "rewrite": "需要重写", "error": "审阅失败"}
    issue_labels = {
        "focus": "重点取舍",
        "redundancy": "重复",
        "wording": "措辞",
        "structure": "结构",
        "logic": "内部逻辑",
        "translation": "翻译腔",
        "format": "格式",
        "other": "其他",
    }
    if report.counts:
        lines.extend(f"- {labels.get(key, key)}：{value}" for key, value in report.counts.items())
    else:
        lines.append("- 没有可审阅稿件")
    lines.extend(["", "### 问题类型", ""])
    type_lines = [(key, count) for key, count in report.issue_counts.items() if not key.startswith("pattern:")]
    lines.extend(f"- {issue_labels.get(key, key)}：{count}" for key, count in type_lines)
    if not type_lines:
        lines.append("- 暂无")
    pattern_lines = [
        (key.removeprefix("pattern:"), count)
        for key, count in report.issue_counts.items()
        if key.startswith("pattern:") and count >= 2
    ][:20]
    lines.extend(["", "### 重复出现的写作模式", ""])
    lines.extend(f"- {key}：{count}" for key, count in pattern_lines)
    if not pattern_lines:
        lines.append("- 暂无（模型给出的模式均只出现 1 次）")

    for index, result in enumerate(report.items, start=1):
        item = result.item
        lines.extend(
            [
                "",
                f"## {index}. {item.title or '未命名快讯'}",
                "",
                f"- task_id：`{item.task_id}`；来源：`{item.source}`；写作时间：{item.write_completed_at or '未知'}",
                f"- 版本：{'最终稿' if item.is_final else '草稿'}",
                f"- 判断：{labels.get(result.verdict or 'error', result.verdict or '审阅失败')}",
            ]
        )
        if result.error:
            lines.extend([f"- 错误：`{result.error}`", ""])
            continue
        lines.extend(["", "### 当前标题", "", item.title, "", "### 当前正文", "", item.content])
        lines.extend(["", "### 审阅意见", "", result.summary or "无需修改。"])
        if result.issues:
            lines.extend(["", "| 位置 | 类型 | 原文 | 建议 | 原因 |", "| --- | --- | --- | --- | --- |"])
            for issue in result.issues:
                lines.append(
                    "| {location} | {type} | {original} | {suggested} | {reason} |".format(
                        **{
                            key: _markdown_cell(
                                issue_labels.get(str(issue.get(key) or ""), str(issue.get(key) or ""))
                                if key == "type"
                                else str(issue.get(key) or "")
                            )
                            for key in ("location", "type", "original", "suggested", "reason")
                        }
                    )
                )
        if result.title_suggestion != item.title:
            lines.extend(["", "### 建议标题", "", result.title_suggestion])
        if result.content_suggestion != item.content:
            lines.extend(["", "### 建议正文", "", result.content_suggestion])
        if result.patterns:
            lines.extend(["", "可复用模式：" + "、".join(result.patterns)])
    return "\n".join(lines).rstrip() + "\n"


def _normalize_result(item: WritingReviewItem, *, model: str, raw_output: str, payload: dict[str, Any]) -> WritingReviewResult:
    verdict = str(payload.get("verdict") or "no_change")
    if verdict not in {"no_change", "minor_edit", "rewrite"}:
        raise ValueError(f"unsupported verdict: {verdict}")
    issues: list[dict[str, str]] = []
    for raw_issue in payload.get("issues") or []:
        if not isinstance(raw_issue, dict):
            continue
        original = _clean_text(raw_issue.get("original"))
        suggested = _clean_text(raw_issue.get("suggested"))
        location = str(raw_issue.get("location") or "content")
        if location not in {"title", "content"} or not original or not suggested:
            continue
        source_text = item.title if location == "title" else item.content
        if original not in source_text:
            continue
        issues.append(
            {
                "location": location,
                "type": str(raw_issue.get("type") or "other"),
                "severity": str(raw_issue.get("severity") or "low"),
                "original": original,
                "suggested": suggested,
                "reason": _clean_text(raw_issue.get("reason")),
            }
        )
    if verdict == "no_change":
        issues = []
        title_suggestion = item.title
        content_suggestion = item.content
    else:
        title_suggestion = _clean_text(payload.get("title_suggestion")) or item.title
        content_suggestion = _clean_text(payload.get("content_suggestion")) or item.content
    return WritingReviewResult(
        item=item,
        model=model,
        raw_output=raw_output,
        verdict=verdict,
        summary=_clean_text(payload.get("summary")),
        title_suggestion=title_suggestion,
        content_suggestion=content_suggestion,
        issues=issues,
        patterns=[_clean_text(value) for value in payload.get("patterns") or [] if _clean_text(value)][:3],
    )


def _parse_payload(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, count=1, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text, count=1).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("writing review output is not a JSON object")
    return payload


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def default_report_paths() -> tuple[Path, Path]:
    output_dir = get_paths().exports_dir / "writing_review"
    return output_dir, output_dir


def default_window(hours: float = 12.0) -> tuple[datetime, datetime]:
    until = datetime.now(UTC)
    return until - timedelta(hours=hours), until
