import json
from decimal import Decimal

import pytest

from packages.hyperliquid_news.episodes import advance_episode


def fill(stamp, before, size, side, *, pnl='0', fee='0', coin='BTC', **extra):
    return {'coin': coin, 'time': stamp, 'startPosition': before, 'sz': size,
            'px': '100', 'side': side, 'closedPnl': pnl, 'fee': fee,
            'feeToken': 'USDC', **extra}


def test_incremental_episode_survives_serialization_and_partial_exits():
    current = None
    for stamp in range(1, 51):
        current, done = advance_episode(current, fill(stamp, str(stamp - 1), '1', 'B', fee='0.1'))
        assert not done
    current = json.loads(json.dumps(current))
    current, done = advance_episode(current, fill(51, '50', '20', 'A', pnl='100', fee='2'))
    assert not done
    current, done = advance_episode(current, fill(52, '30', '30', 'A', pnl='150', fee='3'))
    assert current is None
    episode = done[0]
    assert episode['complete_start'] and episode['continuous']
    assert episode['opening_quantity'] == '50'
    assert episode['closing_quantity'] == '50'
    assert Decimal(episode['closed_pnl']) - Decimal(episode['fees_usd']) == Decimal('240')
    assert episode['fill_count'] == 52


def test_reversal_splits_fee_and_opens_new_episode():
    current, _ = advance_episode(None, fill(1, '0', '2', 'B', fee='1'))
    current, done = advance_episode(current, fill(2, '2', '3', 'A', pnl='8', fee='3'))
    assert done[0]['direction'] == 'long'
    assert done[0]['fees_usd'] == '3'
    assert done[0]['closing_quantity'] == '2'
    assert current['direction'] == 'short'
    assert current['position'] == '-1'
    assert current['fees_usd'] == '1'
    assert current['complete_start']


def test_missing_history_and_gap_do_not_become_complete_rounds():
    current, _ = advance_episode(None, fill(1, '5', '1', 'B'))
    assert not current['complete_start']
    current, done = advance_episode(current, fill(2, '7', '7', 'A'))
    assert not done[0]['continuous'] and done[0]['end_ms'] is None
    assert not done[1]['complete_start'] and done[1]['end_ms'] == 2
    assert current is None


def test_unsupported_fee_and_ambiguous_or_out_of_order_inputs():
    current, _ = advance_episode(None, fill(10, '0', '1', 'B', fee='1', feeToken='HYPE'))
    assert current['unsupported_fee_currency'] and current['fees_usd'] == '0'
    with pytest.raises(ValueError):
        advance_episode(current, fill(9, '1', '1', 'A'))
    with pytest.raises(ValueError):
        advance_episode(current, fill(11, '1', '1', 'A', _ambiguous_order=True))
