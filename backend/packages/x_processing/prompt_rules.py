from __future__ import annotations

from collections.abc import Mapping
from typing import Any


X_ARTICLE_CONTENT_FORMATS = frozenset({"x_post_with_article", "x_article"})
X_ARTICLE_RULE_MARKER = "【X Article专项规则】"

X_ARTICLE_WRITER_RULES = f"""{X_ARTICLE_RULE_MARKER}
当输入 metadata.content_format 为 x_post_with_article 或 x_article 时，执行以下专项规则：
- 外层帖子与 Article 合并为同一条 X 来源，按单一来源编辑；正文只输出一条快讯，不把两个区块分别处理。
- 只保留核心观点、关键事实和数字，输出 2–4 句、1–2 段。
- 禁止逐段翻译或完整复述 Article，不得保留输入区块标题（如“【普通帖子】”“【X文章】”）。
- “发言人在 X 平台发文表示”最多出现一次，只能放在正文开头（将“发言人”替换为实际姓名）；正文后文不得重复来源引导语。
""".strip()

X_ARTICLE_WRITER_CONTEXT = """【X Article写作上下文】
以下材料可能同时包含外层帖子和 X Article 的完整正文，但它们属于同一条 X 来源。请将两部分合并编辑成一条快讯；完整 Article 仅作为事实材料和审计上下文，不应直接成为发布正文。"""


def is_x_article_content_format(metadata: Mapping[str, Any] | None) -> bool:
    return bool(metadata and metadata.get("content_format") in X_ARTICLE_CONTENT_FORMATS)
