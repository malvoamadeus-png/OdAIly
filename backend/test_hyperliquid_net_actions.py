"""Redacted production replay: 2026-10-30 account round trips."""
import json
from pathlib import Path
from dataclasses import asdict

from packages.hyperliquid_news.account_monitor import account_event
from packages.hyperliquid_news.copy import format_news, _account_action_detail
from packages.hyperliquid_news.models import Candidate
from packages.hyperliquid_news.production import production_allowed
from packages.x_processing.models import DraftBrief


def samples():
    return json.loads((Path(__file__).parent/'fixtures/hyperliquid_account_roundtrip.json').read_text())


def test_roundtrip_and_small_net_change_do_not_qualify_account_action():
    zec, eth, hype = samples()
    assert account_event(Candidate(**eth)) is None
    assert account_event(Candidate(**zec)) is None
    assert account_event(Candidate(**hype)) is not None


def test_dispatch_rechecks_old_account_events_against_net_threshold():
    zec, eth, hype = samples()
    policy = {'enabled': True, 'started_ms': 1, 'enabled_scenarios': ['account_profit']}
    assert not production_allowed(eth, policy)
    assert not production_allowed(zec, policy)
    assert production_allowed(hype, policy)


def test_account_title_summarizes_net_exposure_instead_of_gross_legs():
    hype = samples()[2]
    result = format_news(DraftBrief(
        title='总盈利约2267万美元，某地址平仓约1823万美元HYPE多单并开仓约1047万美元HYPE多单',
        content='某地址合约账户累计收益约2267万美元。其减少HYPE多单。'), hype)
    assert '减少约803万美元HYPE多单' in result.title
    assert '开仓' not in result.title
    assert '平仓' not in result.title


def test_missing_chronology_never_invents_close_then_open():
    eth = samples()[1]
    detail = _account_action_detail(eth)
    assert '后' not in detail
    assert '再' not in detail


def test_execution_order_survives_detector_and_writer_material():
    from packages.hyperliquid_news.detector import evaluate_address
    from packages.hyperliquid_news.copy import writer_material
    from packages.hyperliquid_news.models import Rules
    fills = [
        {'coin': 'ETH', 'time': 1000, 'tid': 9, 'startPosition': '0', 'sz': '10000',
         'side': 'A', 'px': '2265', 'closedPnl': '0'},
        {'coin': 'ETH', 'time': 2000, 'tid': 1, 'startPosition': '-10000', 'sz': '10000',
         'side': 'B', 'px': '2207', 'closedPnl': '5800000'}]
    events, _ = evaluate_address(address='0x'+'1'*40, observed_ms=3000, first_seen_ms=0,
        positions={}, previous={'observed_ms': 0, 'positions': {}}, markets={},
        history={'official_account_performance': {'windows': {'allTime': {'pnl': '22670000'}}}},
        new_fills=list(reversed(fills)), rules=Rules())
    event = asdict(events[0])
    facts = writer_material(event)['facts']
    assert [leg['action'] for leg in facts['actions']['execution_sequence']] == ['open_short', 'close_short']
    assert facts['current_position_status'] == '空仓'
    assert account_event(events[0]) is None


def test_contradictory_model_chronology_is_rejected():
    import pytest
    from packages.hyperliquid_news.copy import normalize_account_copy
    eth = samples()[1]
    eth['evidence']['actions']['execution_sequence'] = [
        {'action': 'open_short'}, {'action': 'close_short'}]
    with pytest.raises(ValueError, match='chronology'):
        normalize_account_copy(DraftBrief(title='某地址操作ETH',
            content='某地址平仓约2207万美元ETH空单后再开仓约2266万美元ETH空单。'), eth)


def test_price_changes_cannot_turn_unchanged_exposure_into_net_addition():
    from packages.hyperliquid_news.actions import snapshot_net_change, execution_sequence
    event = samples()[2]
    event['evidence']['position']['size'] = event['evidence']['previous_position']['size']
    assert float(snapshot_net_change(event['evidence'])['estimated_value']) == 0
    assert account_event(Candidate(**event)) is None
    # A missing fill cannot support a chronological account of the operation.
    assert execution_sequence([
        {'time': 1, 'startPosition': '0', 'sz': '2', 'side': 'A', 'px': '100'},
        {'time': 2, 'startPosition': '-3', 'sz': '3', 'side': 'B', 'px': '100'}]) is None


def test_flat_final_snapshot_is_explicit_in_account_body():
    from packages.hyperliquid_news.copy import normalize_account_copy
    eth = samples()[1]
    draft = normalize_account_copy(DraftBrief(title='某地址调整ETH仓位',
        content='某地址合约账户累计收益约2267万美元。窗口内开仓约2266万美元ETH空单，并平仓约2207万美元ETH空单。'), eth)
    assert '其已无ETH仓位' in draft.content
    assert normalize_account_copy(draft, eth).content.count('已无ETH仓位') == 1
