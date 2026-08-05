import json

from packages.auditor.models import AuditorTask
from packages.auditor.prompts import parse_auditor_output
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief


def _task(content: str) -> AuditorTask:
    return AuditorTask(
        id=1,
        source_item_id="item-1",
        source_url=None,
        title="测试标题",
        content=content,
        content_hash="hash",
        published_at=None,
    )


def test_writer2_removes_native_asset_suffix_before_space_normalization() -> None:
    result = format_brief(
        DraftBrief(
            title="bitcoin:native突破新高",
            content="bitcoin:native上涨100美元，dapp中文。",
        )
    )

    assert result.title == "bitcoin突破新高"
    assert result.content == "Odaily星球日报讯 bitcoin 上涨 100 美元，DApp 中文。"
    assert ":native" not in result.title
    assert ":native" not in result.content


def test_writer2_reapplies_fixed_account_spacing_exception() -> None:
    result = format_brief(
        DraftBrief(
            title="Jason60704294发布消息",
            content="Jason60704294表示100USDT。",
        )
    )

    assert result.title == "“先定10个大目标”发布消息"
    assert result.content == "Odaily星球日报讯 “先定10个大目标”表示 100 USDT。"


def test_auditor_ignores_borrow_lend_word_usage() -> None:
    task = _task("即用户重复抵押资产、借入稳定币并再次部署资金")
    raw_output = json.dumps(
        {
            "has_issue": True,
            "severity": "medium",
            "issues": [
                {
                    "type": "grammar",
                    "location": "content",
                    "original": "借入稳定币",
                    "suggested": "购入稳定币",
                    "reason": "借入用词不当",
                    "evidence": "",
                }
            ],
            "summary": "借入用词疑似错误",
        },
        ensure_ascii=False,
    )

    result = parse_auditor_output(raw_output, task)

    assert result.has_issue is False
    assert result.issues == []
    assert result.summary == ""


def test_auditor_ignores_borrow_lend_rewrite_in_either_direction() -> None:
    task = _task("协议允许用户借出资产获取收益")
    raw_output = json.dumps(
        {
            "has_issue": True,
            "severity": "low",
            "issues": [
                {
                    "type": "typo",
                    "location": "content",
                    "original": "借出资产",
                    "suggested": "卖出资产",
                    "reason": "动作词错误",
                    "evidence": "",
                }
            ],
            "summary": "借出应改为卖出",
        },
        ensure_ascii=False,
    )

    result = parse_auditor_output(raw_output, task)

    assert result.has_issue is False
