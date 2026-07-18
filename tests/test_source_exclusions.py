from __future__ import annotations

from packages.common.source_exclusions import (
    SourceExclusionRuleGroup,
    is_source_excluded,
    media_source_exclusion_scopes,
    normalize_exclusion_text,
)


def _group(
    *,
    scopes: tuple[str, ...] = ("crypto_source",),
    terms: tuple[str, ...] = ("Ripple", "XRP", "RLUSD"),
    enabled: bool = True,
) -> SourceExclusionRuleGroup:
    return SourceExclusionRuleGroup(
        rule_key="ripple-commercial",
        name="Ripple / XRP commercial content",
        description="",
        scopes=scopes,
        terms=terms,
        enabled=enabled,
    )


def test_normalization_is_nfkc_case_insensitive_and_whitespace_stable() -> None:
    assert normalize_exclusion_text("  ＲＩＰＰＬＥ\n Labs  ") == "ripple labs"


def test_any_term_matches_title_summary_or_body_as_a_substring() -> None:
    assert is_source_excluded(
        [_group()],
        scopes=["crypto_source"],
        texts=["Veteran grants", "Paid in rlUsd", None],
    )


def test_disabled_group_and_unselected_scope_do_not_match() -> None:
    assert not is_source_excluded(
        [_group(enabled=False)],
        scopes=["crypto_source"],
        texts=["Ripple funds a business program"],
    )
    assert not is_source_excluded(
        [_group()],
        scopes=["competitor"],
        texts=["Ripple funds a business program"],
    )


def test_ripple_group_blocks_crypto_source_without_affecting_x() -> None:
    groups = [_group()]
    text = ["Ripple commits $250,000 to veteran-owned businesses"]
    assert is_source_excluded(groups, scopes=["crypto_source"], texts=text)
    assert not is_source_excluded(groups, scopes=["x"], texts=text)


def test_mixed_source_uses_raw_then_classified_target_scope() -> None:
    groups = [
        _group(scopes=("mixed_source",), terms=("sponsored",)),
        _group(scopes=("crypto_source",), terms=("Ripple",)),
    ]
    assert media_source_exclusion_scopes("mixed_source") == ("mixed_source",)
    assert media_source_exclusion_scopes("mixed_source", classified_target="crypto") == (
        "mixed_source",
        "crypto_source",
    )
    assert is_source_excluded(
        groups,
        scopes=media_source_exclusion_scopes("mixed_source"),
        texts=["Sponsored industry update"],
    )
    assert is_source_excluded(
        groups,
        scopes=media_source_exclusion_scopes("mixed_source", classified_target="crypto"),
        texts=["Ripple business update"],
    )

