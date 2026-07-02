from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from packages.common.paths import ensure_runtime_dirs, get_paths
from .models import AI_SOURCE, PipelineRecord, TaskRecord


PublisherProfileKey = Literal["regular", "ai_source"]
PublisherRuleKind = Literal["allow", "deny"]
PublisherDecision = Literal["pass", "reject"]


class PublisherRule(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool = False
    examples: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("id 不能为空")
        return text

    @field_validator("name", "description")
    @classmethod
    def normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("examples")
    @classmethod
    def normalize_examples(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def require_enabled_rule_content(self) -> "PublisherRule":
        if self.enabled and (not self.name or not self.description):
            raise ValueError("启用规则必须填写规则名和规则说明")
        return self


class PublisherRuleProfile(BaseModel):
    key: PublisherProfileKey
    label: str
    enabled: bool = False
    note: str = ""
    allow_rules: list[PublisherRule] = Field(default_factory=list)
    deny_rules: list[PublisherRule] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("label 不能为空")
        return text


class PublisherRuleConfig(BaseModel):
    version: int = 1
    regular: PublisherRuleProfile
    ai_source: PublisherRuleProfile
    updated_at: str | None = None
    updated_by: str | None = None


PUBLISHER_DECISION_SCHEMA = {
    "type": "json_schema",
    "name": "publisher_decision",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["pass", "reject"],
            },
        },
        "required": ["decision"],
    },
    "strict": True,
}


DEFAULT_REGULAR_ALLOW_RULES = [
    PublisherRule(
        id="allow_onchain_whale_liquidation",
        name="链上大额 / 巨鲸 / 清算",
        description="放行链上大额转账、巨鲸地址动作、持仓或增减持、储备变化、清算等有明确事实和主体的链上资金事件。",
        enabled=True,
        examples=[
            "某巨鲸向交易所转入 1,000 枚 BTC",
            "某地址因 ETH 下跌被清算约 800 万美元",
            "某机构钱包增持 5,000 枚 ETH",
        ],
    ),
    PublisherRule(
        id="allow_crypto_policy_regulation",
        name="政策 / 监管 / 法案",
        description="放行与 Crypto、稳定币、交易所、DeFi、数字资产等直接相关的政策、监管、执法、法案、监管框架信息。",
        enabled=True,
        examples=[
            "EBA 公布欧盟稳定币罚款框架",
            "某监管机构批准加密交易平台牌照",
        ],
    ),
    PublisherRule(
        id="allow_crypto_product_launch",
        name="产品发布 / 升级 / 上线",
        description="放行 Crypto 相关产品、功能、代币、协议、交易服务、钱包、链上应用的发布、升级、上线或开放使用。",
        enabled=True,
        examples=[
            "某交易所上线某代币现货交易",
            "某钱包推出链上支付新功能",
        ],
    ),
    PublisherRule(
        id="allow_macro_rates_central_bank",
        name="宏观数据 / 利率 / 央行",
        description="放行重要宏观数据、利率决议、央行表态、货币政策等会影响金融市场预期的信息。",
        enabled=True,
        examples=[
            "美联储宣布维持利率不变",
            "美国 CPI 同比低于市场预期",
        ],
    ),
    PublisherRule(
        id="allow_institution_people_view",
        name="机构观点 / 人物表态",
        description=(
            "仅放行高重要性主体的观点或表态，且内容必须直接涉及 Crypto、监管、市场结构、交易基础设施、"
            "稳定币、ETF、托管清算等核心议题，并且包含具体新信息、新动作、新数据或明确政策信号。"
            "纯态度表达、吹捧、自我宣传、泛政治评论、宏大叙事、空泛站队、无新增事实的判断一律不自动发布。"
            "如果无法从正文中明确看出主体具备高重要性，或无法明确看出与 Crypto/监管/市场结构直接相关，默认 reject。"
        ),
        enabled=True,
        examples=[
            "美联储官员表示若通胀继续回落将支持年内降息",
            "Coinbase CEO 表示公司已就某项稳定币监管草案向国会提交正式建议",
        ],
    ),
    PublisherRule(
        id="allow_funding",
        name="融资",
        description="放行融资、投资、基金募集、估值、投资方参与等融资类信息。",
        enabled=True,
        examples=[
            "某项目完成 1000 万美元 A 轮融资",
            "某基金宣布募资 2 亿美元投资加密基础设施",
        ],
    ),
    PublisherRule(
        id="allow_security_incident_outage",
        name="安全 / 事故 / 停运",
        description="放行黑客攻击、被盗、漏洞、链或服务事故、重大宕机、停运等风险事件。",
        enabled=True,
        examples=[
            "某协议疑似遭攻击损失约 500 万美元",
            "某网络因故障暂停出块",
        ],
    ),
    PublisherRule(
        id="allow_project_org_major_move",
        name="项目 / 组织重大动向",
        description=(
            "仅放行具备明确新闻价值的重大组织变化，例如头部或关键基础设施机构的裁员、重组、关键高管变动、"
            "项目停运、业务关闭、产品停止维护、重大转向等。普通中小主体的常规组织更新、例行任命、常规扩张、"
            "routine 业务推进不自动发布。"
        ),
        enabled=True,
        examples=[
            "Robinhood 近期裁员",
            "BitGo 削减 15% 员工",
            "某项目宣布停止运营并关闭服务",
        ],
    ),
]


DEFAULT_REGULAR_DENY_RULES = [
    PublisherRule(
        id="deny_self_promotion_endorsement",
        name="自我宣传 / 站台型表达",
        description=(
            "排除主体自夸、自我背书、互相吹捧、站台转发、品牌宣传、姿态展示、空泛喊话等没有新增事实、"
            "没有明确动作、没有可执行信息的表达。即使发言人知名，只要本质是宣传或站台，也必须 reject。"
        ),
        enabled=True,
        examples=[
            "某创始人称很高兴看到某基金会正在审慎处理关键问题",
            "某项目方表示行业终于开始理解我们的路线",
        ],
    ),
    PublisherRule(
        id="deny_geopolitics",
        name="地缘政治",
        description="排除纯地缘政治、战争、外交、能源出口等与 Crypto 或金融市场核心链路无直接关系的信息。",
        enabled=True,
        examples=["普京：俄罗斯正讨论对柴油出口实施全面禁令"],
    ),
    PublisherRule(
        id="deny_market_price_move",
        name="市场行情 / 价格异动",
        description="排除单纯价格涨跌、突破、短时波动、行情播报、无额外事实支撑的价格异动。",
        enabled=True,
        examples=["BTC 突破 70000 USDT", "某代币 1 小时上涨 20%"],
    ),
    PublisherRule(
        id="deny_non_crypto_macro_political_commentary",
        name="非 Crypto 的宏大政治评论",
        description=(
            "排除与 Crypto、数字资产监管、稳定币、交易市场结构没有直接关系的宏大政治、财政、宪政、文明叙事、"
            "意识形态评论。即使发言人来自加密行业，只要内容本身不是直接的 Crypto 新闻，也必须 reject。"
        ),
        enabled=True,
        examples=[
            "某交易所 CEO 讨论美国宪法应增加政府支出上限",
            "某投资人谈民主制度终将因债务而失败",
        ],
    ),
    PublisherRule(
        id="deny_incentive_grant_activity",
        name="激励 / 赠款 / 活动",
        description="排除明确指向交易大赛、奖励、空投活动、赠款计划、营销活动、社区活动的信息。",
        enabled=True,
        examples=["某交易所开展交易大赛，奖池 10 万美元"],
    ),
    PublisherRule(
        id="deny_strategic_partnership",
        name="战略合作",
        description="排除普通战略合作、生态合作、合作备忘录等缺少实质进展的信息。",
        enabled=True,
        examples=["某项目与某公司达成战略合作"],
    ),
    PublisherRule(
        id="deny_routine_small_entity_company_update",
        name="中小主体例行公司新闻",
        description=(
            "排除普通中小或长尾主体的例行公司新闻，包括常规牌照/审批获批、普通合作、一般扩区、常规产品更新、"
            "例行组织调整、普通上新等。只有当正文能明确证明该主体具备高重要性，或该事件对市场准入、监管格局、"
            "核心基础设施、广泛用户资产安全产生显著影响时，才可不按本规则 reject；否则默认挂后台。"
        ),
        enabled=True,
        examples=[
            "某不知名平台获欧洲某地牌照",
            "某小型金融科技公司宣布拓展至新市场",
        ],
    ),
    PublisherRule(
        id="deny_niche_tradfi_product_move",
        name="小众传统金融产品异动",
        description=(
            "排除与 Crypto 没有直接关系的小众传统金融产品、冷门 ETF、单一基金、个别衍生品、非核心交易工具的"
            "异常流入流出、规模变化、溢价波动或观察性点评。除非它明确影响加密市场结构或代表重大制度变化，否则 reject。"
        ),
        enabled=True,
        examples=[
            "某冷门 ETF 单日流入 6 亿美元",
            "某传统基金规模突然扩大四倍",
        ],
    ),
    PublisherRule(
        id="deny_niche_research_model_publication",
        name="冷门论文 / 估值模型 / 研究发布",
        description=(
            "排除冷门论文发表、估值模型争论、学术研究发布、方法论阐释、研究者个人理论更新等低普适性内容。"
            "除非正文能明确证明其带来监管落地、产品上线、市场结构变化或被核心机构正式采用，否则 reject。"
        ),
        enabled=True,
        examples=[
            "某比特币价格幂律论文发表于期刊",
            "研究者发布新的链上估值模型说明",
        ],
    ),
]


def default_publisher_rule_config() -> PublisherRuleConfig:
    return PublisherRuleConfig(
        regular=PublisherRuleProfile(
            key="regular",
            label="常规",
            enabled=True,
            note="适用于 X、Crypto信源、竞品、金十等非 AI 信源链路。",
            allow_rules=list(DEFAULT_REGULAR_ALLOW_RULES),
            deny_rules=list(DEFAULT_REGULAR_DENY_RULES),
        ),
        ai_source=PublisherRuleProfile(
            key="ai_source",
            label="AI信源",
            enabled=False,
            note="暂未启用。当前 AI 信源进入发布者时只挂后台，不自动放行。",
            allow_rules=[],
            deny_rules=[],
        ),
        updated_at=None,
        updated_by=None,
    )


def get_publisher_rule_config_path() -> Path:
    paths = get_paths()
    ensure_runtime_dirs(paths)
    configured = os.getenv("PUBLISHER_RULE_CONFIG_PATH")
    return Path(configured) if configured else paths.config_dir / "publisher_rules.json"


def load_publisher_rule_config(path: Path | None = None) -> PublisherRuleConfig:
    config_path = path or get_publisher_rule_config_path()
    if not config_path.exists():
        return default_publisher_rule_config()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return PublisherRuleConfig.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        return default_publisher_rule_config()


def save_publisher_rule_config(
    config: PublisherRuleConfig,
    *,
    updated_by: str | None = None,
    path: Path | None = None,
) -> PublisherRuleConfig:
    now = datetime.now(UTC).isoformat()
    next_config = config.model_copy(update={"updated_at": now, "updated_by": updated_by})
    config_path = path or get_publisher_rule_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_suffix(f"{config_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(next_config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    return next_config


def profile_key_for_task(task: TaskRecord) -> PublisherProfileKey | None:
    if task.source == AI_SOURCE:
        return "ai_source"
    return "regular"


def get_profile(config: PublisherRuleConfig, key: PublisherProfileKey) -> PublisherRuleProfile:
    return config.ai_source if key == "ai_source" else config.regular


def enabled_rules(profile: PublisherRuleProfile, kind: PublisherRuleKind) -> list[PublisherRule]:
    rules = profile.allow_rules if kind == "allow" else profile.deny_rules
    return [rule for rule in rules if rule.enabled]


def build_publisher_rule_prompt(
    *,
    task: TaskRecord,
    pipeline: PipelineRecord,
    profile: PublisherRuleProfile,
) -> str:
    allow_rules = enabled_rules(profile, "allow")
    deny_rules = enabled_rules(profile, "deny")
    return "\n".join(
        [
            "你是 Odaily 快讯发布者。你的任务是判断一条已定稿快讯是否允许自动发布到前台。",
            "",
            "只输出 JSON，不输出解释文本。格式必须为：",
            '{"decision":"pass|reject"}',
            "",
            "硬性规则：",
            "- 排除规则优先：只要命中任意启用的排除规则，必须输出 reject。",
            "- 如果没有命中排除规则，但命中任意启用的通过规则，输出 pass。",
            "- 如果无法明确归入任意启用的通过规则，输出 reject。",
            "- 案例只是判断参考，不要求逐字匹配。",
            "- 不允许凭常识脑补主体重要性。只有正文直接给出高重要性证据，或主体明显属于国家级监管、央行、头部交易基础设施、头部稳定币/ETF/托管/核心公链基金会等系统性主体时，才可按高重要主体理解。",
            "- 对牌照、审批、合作、组织动向、产品更新等公司新闻，若主体看起来是中小或长尾实体，且正文没有明确写出其市场地位、用户规模、资产规模、监管层级、覆盖范围或系统性影响，默认按 routine company update 处理并 reject。",
            "- 对观点类内容，必须同时满足：高重要主体、直接 Crypto/监管/市场结构相关、含具体新信息/新动作/新数据；三者缺一即 reject。",
            "",
            "【启用的排除规则】",
            format_rules_for_prompt(deny_rules) or "无",
            "",
            "【启用的通过规则】",
            format_rules_for_prompt(allow_rules) or "无",
            "",
            "【来源信息】",
            f"来源：{task.source}",
            f"原始标题：{task.title or ''}",
            f"原始内容：{task.content}",
            "",
            "【已定稿快讯】",
            f"标题：{pipeline.final_title or ''}",
            f"正文：{pipeline.final_content or ''}",
        ]
    )


def format_rules_for_prompt(rules: list[PublisherRule]) -> str:
    blocks: list[str] = []
    for index, rule in enumerate(rules, start=1):
        lines = [f"{index}. {rule.name}", f"说明：{rule.description}"]
        if rule.examples:
            lines.append("案例：")
            lines.extend(f"- {example}" for example in rule.examples)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_publisher_decision(value: str) -> PublisherDecision:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("publisher output must be a JSON object")
    decision = str(payload.get("decision") or "").strip()
    if decision not in {"pass", "reject"}:
        raise ValueError(f"invalid publisher decision: {decision}")
    return decision  # type: ignore[return-value]


def publisher_config_to_prompt_text(config: PublisherRuleConfig) -> str:
    sample_task = TaskRecord(id=0, source="config_preview", source_item_id="preview", source_url=None, title="", content="")
    sample_pipeline = PipelineRecord(task_id=0, final_title="", final_content="")
    return build_publisher_rule_prompt(task=sample_task, pipeline=sample_pipeline, profile=config.regular)


def publisher_rule_config_payload(config: PublisherRuleConfig) -> dict[str, Any]:
    return {
        "config": config.model_dump(mode="json"),
        "prompt_text": publisher_config_to_prompt_text(config),
    }
