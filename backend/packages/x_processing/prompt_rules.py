from __future__ import annotations

import re
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


EVENT_TRACKING_WRITER_RULES = """【热点自动快讯官方进展写作规则】
本次输入是已核验官方账号对正在追踪事件发布的进展，不是第三方链上监测播报。
以下规则覆盖基础模板中关于“据{发言人}监测”及来源开头的要求；news_type=onchain 只是内容分类，不代表作者是监测机构。
- AI 直接生成有主语、有归属的完整正文；格式化阶段只添加 Odaily 刊头和整理字符，不补发言人或“据某某监测”。
- 使用真实发布人及原文明确的机构/项目身份，不能把事件标题当作发言人。正文开头用“项目/机构在 X 平台发文表示……”或“项目/机构公布……”，根据原文动作选择；不得以“据某某监测”开头。
- 同一来源开头只归属一次，禁止“据 PaperTrade 监测，PaperTrade 表示……”或“据项目方公告，项目方表示……”等叠加。后文用“其”“团队”等自然衔接；需要区分其他来源或明确主张归属时可再次点出主体。
- 标题和首段优先写本次实质进展与相对既有时间线的变化；无关宣传、上线成绩和自我评价不要挤占核心事件。不要逐段翻译完整官方长帖。
- 事件进展摘要和已确认/未确认材料仅供选题与归属参考，以官方原帖为事实边界；“官方确认自己如此表态”不等于第三方独立证实。对官方的解释、争议判断和未获独立验证的说法保留“称/表示”等归属，不改写为客观定论，不补原帖没有的信息。
""".strip()


def validate_event_tracking_attribution(content: str) -> None:
    # Fail in write, where the job retry can regenerate the draft.  Silently
    # deleting an attribution during formatting could change who owns a claim.
    body = content.strip().removeprefix("Odaily星球日报讯").lstrip()
    if re.match(r"^据[^，,。；;\n]{1,100}监测\s*[，,：:]", body):
        raise ValueError("event tracking official statement has a forbidden monitoring attribution")
