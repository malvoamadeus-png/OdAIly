from decimal import Decimal
import random

import pytest

from packages.hyperliquid_news.compact_history import compact_history, estimate_compaction, extend_compact, summarize_compact, verify_compact_history
from packages.hyperliquid_news.history import summarize


def fill(tid, stamp, before, size, side, *, coin='BTC', pnl='0', fee='0', **extra):
    return {'tid': tid, 'coin': coin, 'time': stamp, 'startPosition': before,
            'sz': size, 'side': side, 'px': '100', 'closedPnl': pnl,
            'fee': fee, 'feeToken': 'USDC', **extra}


def assert_equivalent(original, compact):
    assert original.keys() == compact.keys()
    for key in original.keys() - {'assets', 'account_closed_pnl_less_fees'}:
        assert original[key] == compact[key], key
    if original['account_closed_pnl_less_fees'] is None:
        assert compact['account_closed_pnl_less_fees'] is None
    else:
        assert Decimal(original['account_closed_pnl_less_fees']) == Decimal(compact['account_closed_pnl_less_fees'])
    assert original['assets'].keys() == compact['assets'].keys()
    for coin, asset in original['assets'].items():
        actual = compact['assets'][coin]
        for key in asset.keys() - {'closed_pnl_less_fees', 'completed_rounds', 'completed_round_pnl'}:
            assert asset[key] == actual[key], (coin, key)
        assert Decimal(asset['closed_pnl_less_fees']) == Decimal(actual['closed_pnl_less_fees'])
        assert [Decimal(x) for x in asset['completed_round_pnl']] == [Decimal(x) for x in actual['completed_round_pnl']]
        assert len(asset['completed_rounds']) == len(actual['completed_rounds'])
        for left, right in zip(asset['completed_rounds'], actual['completed_rounds']):
            assert left['end_ms'] == right['end_ms']
            assert Decimal(left['pnl']) == Decimal(right['pnl'])
            assert Decimal(left['close_notional']) == Decimal(right['close_notional'])


def test_all_window_boundaries_match_original_with_reversal_and_gaps():
    rows = [
        fill(1, 10, '0', '2', 'B', fee='0.1'),
        fill(2, 20, '2', '1', 'B', fee='0.2'),
        fill(3, 30, '3', '1', 'A', pnl='4', fee='0.3'),
        fill(4, 40, '2', '3', 'A', pnl='10', fee='0.3'),
        fill(5, 50, '-1', '1', 'B', pnl='-3', fee='0.1'),
        fill(6, 60, '3', '3', 'A', pnl='5', feeToken='HYPE', fee='1'),
        fill(7, 70, '0', '2', 'B', coin='ETH'),
        fill(8, 80, '2', '2', 'A', coin='ETH', pnl='11'),
    ]
    compact = compact_history(rows + [rows[0]])
    for start in (0, 10, 15, 30, 40, 50, 55, 60, 90):
        for end in (10, 30, 40, 50, 60, 80, 90):
            if start <= end:
                verify_compact_history(rows, compact, [(start, end)])


def test_same_timestamp_aggregates_and_ambiguous_order_is_preserved():
    rows = [fill(1, 10, '0', '1', 'B'), fill(2, 10, '1', '1', 'B'),
            fill(3, 20, '2', '2', 'A', pnl='3')]
    compact = compact_history(rows)
    assert len(compact['assets']['BTC']['moments']) == 2
    assert len(compact['assets']['BTC']['episodes']) == 1
    assert_equivalent(summarize(rows, start_ms=0, end_ms=20),
                      summarize_compact(compact, start_ms=0, end_ms=20))
    ambiguous = [fill(4, 10, '0', '1', 'B'), fill(5, 10, '0', '2', 'B')]
    data = compact_history(ambiguous)
    assert_equivalent(summarize(ambiguous, start_ms=0, end_ms=20),
                      summarize_compact(data, start_ms=0, end_ms=20))
    assert summarize_compact(data, start_ms=0, end_ms=20)['assets']['BTC']['ambiguous_order_fills'] == 2


def test_order_can_use_history_outside_the_window():
    rows = [fill(1, 1, '0', '1', 'B'),
            fill(2, 2, '1', '1', 'B'), fill(3, 2, '2', '1', 'A')]
    data = compact_history(rows)
    verify_compact_history(rows, data, [(0, 2), (2, 2)])


def test_batch_order_can_use_prior_position():
    data = compact_history([fill(1, 1, '0', '1', 'B')])
    data = extend_compact(data, [fill(2, 2, '1', '1', 'B'), fill(3, 2, '2', '1', 'A')])
    assert data['assets']['BTC']['moments'][-1]['ambiguous_count'] == 0


def test_ambiguous_batch_resets_ordering_context_for_next_batch():
    rows = [fill(1, 1, '0', '1', 'B'), fill(2, 1, '0', '2', 'B'),
            fill(3, 2, '2', '1', 'A'), fill(4, 2, '1', '1', 'B')]
    data = extend_compact(compact_history(rows[:2]), rows[2:])
    verify_compact_history(rows, data, [(0, 2), (2, 2)])


def test_unexpected_opening_pnl_is_not_silently_discarded():
    with pytest.raises(ValueError, match='Unexpected opening PnL'):
        compact_history([fill(1, 1, '0', '1', 'B', pnl='5')])


def test_verifier_rejects_corrupted_compacted_pnl():
    rows = [fill(1, 10, '0', '1', 'B'), fill(2, 20, '1', '1', 'A', pnl='3')]
    compact = compact_history(rows)
    verify_compact_history(rows, compact, [(0, 20), (10, 10), (11, 20)])
    compact['assets']['BTC']['moments'][1]['pnl_less_fees'] = '4'
    with pytest.raises(ValueError, match='differs from source'):
        verify_compact_history(rows, compact, [(0, 20)])


def test_fractional_reversal_fees_match_existing_calculation():
    rows = [fill(1, 1, '0', '1', 'B', fee='1'),
            fill(2, 2, '1', '3', 'A', pnl='10', fee='1'),
            fill(3, 3, '-2', '2', 'B', pnl='3', fee='1')]
    compact = compact_history(rows)
    verify_compact_history(rows, compact, [(0, 3), (2, 3)])


def test_estimate_counts_unique_fills_and_excludes_spot():
    row = fill(1, 10, '0', '1', 'B')
    rows = [row, row, fill(2, 10, '0', '1', 'B', coin='@1')]
    estimate = estimate_compaction(rows, compact_history(rows))
    assert estimate['source_fills'] == 1
    assert estimate['moments'] == 1 and estimate['episodes'] == 1
    assert estimate['source_payload_bytes'] > 0 and estimate['compact_payload_bytes'] > 0


def test_incremental_batches_resume_open_position_and_reversal():
    rows = [fill(1, 1, '0', '2', 'B', fee='0.1'),
            fill(2, 2, '2', '3', 'A', pnl='5', fee='0.3'),
            fill(3, 3, '-1', '1', 'B', pnl='-2')]
    data = compact_history(rows[:1])
    data = extend_compact(data, rows[1:2])
    data = extend_compact(data, rows[2:])
    verify_compact_history(rows, data, [(0, 3), (2, 3), (1, 2)])


def test_incremental_late_batch_does_not_change_original_data():
    data = compact_history([fill(1, 10, '0', '1', 'B')])
    with pytest.raises(ValueError, match='Late or same-timestamp'):
        extend_compact(data, [fill(2, 10, '1', '1', 'A')])
    assert data['assets']['BTC']['episodes'][0]['position'] == '1'
    assert len(data['assets']['BTC']['moments']) == 1


def test_varied_trade_sequences_match_all_window_boundaries():
    randomizer = random.Random(4681)
    for sample in range(20):
        position = 0
        rows = []
        for index in range(24):
            delta = randomizer.choice([-4, -3, -2, -1, 1, 2, 3, 4])
            pnl = str(randomizer.randint(-20, 20)) if position * delta < 0 else '0'
            rows.append(fill(index, index + 1, str(position), str(abs(delta)),
                             'B' if delta > 0 else 'A', pnl=pnl,
                             fee=str(randomizer.randint(0, 9) / 10)))
            position += delta
        data = compact_history(rows)
        for start in range(0, 25, 3):
            for end in range(start, 25, 4):
                try:
                    verify_compact_history(rows, data, [(start, end)])
                except ValueError as exc:
                    raise AssertionError(f'sample {sample}, window {start}..{end}') from exc
