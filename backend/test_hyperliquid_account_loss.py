from decimal import Decimal
import pytest
from packages.hyperliquid_news.account_loss import DAY, loss_performance, loss_qualified, replay_loss_actions
from packages.hyperliquid_news.copy import writer_material, format_news
from packages.hyperliquid_news.detector import highlights
from packages.hyperliquid_news.models import Rules
from packages.x_processing.models import DraftBrief

A='0x'+'1'*40
END=20*DAY

def curve(values, name='perpMonth'):
    return [[name, {'pnlHistory': [[END-(len(values)-1-i)*DAY,str(p)] for i,p in enumerate(values)]}]]

def action(coin='ETH', before='0', size='2000', side='B', stamp=END+1):
    return {'coin':coin,'startPosition':before,'sz':size,'side':side,'time':stamp,
            'tid':str(stamp),'px':'3000','closedPnl':'0','fee':'0','feeToken':'USDC'}

def test_account_loss_independent_of_action_asset_and_negative_single_asset_insufficient():
    history={'official_account_loss':loss_performance(curve([-25000000], 'perpAllTime'), cutoff_ms=END)}
    for coin in ('BTC','ETH','xyz:CL'):
        assert 'historical_loss' in highlights(history,coin,Rules())
    assert 'historical_loss' not in highlights({'assets':{'ETH':{
        'closed_pnl_less_fees':'-100000000','consecutive_losing_rounds':20}}},'ETH',Rules())

@pytest.mark.parametrize('values,expected',[
    ([10000000,9000000,8000000,7000000],False),  # Still lifetime profitable.
    ([-1000000,-2000000,-3000000,-4000000],False),
    ([0,-1000000,0,-3000000],False),  # Recovery interrupts daily decline.
    ([0,-1000000,-1000000,-3000000],False),
    ([0,-100,-200,-300],False),
    ([0,-1000000,-3000000],False)])
def test_daily_curve_decline(values,expected):
    p=loss_performance(curve(values), cutoff_ms=END)
    assert loss_qualified(p,Rules()) is expected

def test_no_future_sparse_spot_or_stale_curve():
    payload=curve([0,-1000000,-2000000,-3000000])
    assert not loss_qualified(loss_performance(payload,cutoff_ms=END-DAY),Rules())
    assert not loss_qualified(loss_performance(curve([0,-1000000,-2000000,-3000000],'month'),cutoff_ms=END),Rules())
    assert not loss_qualified(loss_performance(payload,cutoff_ms=END+2*DAY),Rules())
    payload[0][1]['pnlHistory'].pop(1)
    assert not loss_qualified(loss_performance(payload,cutoff_ms=END),Rules())

def test_action_cutoff_and_important_action_only():
    payload=curve([-25000000],'perpAllTime')
    assert not replay_loss_actions(A,[action(stamp=END)],payload)
    assert not replay_loss_actions(A,[action(size='1')],payload)
    assert len(replay_loss_actions(A,[action()],payload)) == 1
    assert not replay_loss_actions(A,[],payload)

@pytest.mark.parametrize('before,size,side,field',[
    ('0','2000','B','open_long_notional'),('2000','2000','B','open_long_notional'),
    ('4000','2000','A','close_long_notional'),('2000','2000','A','close_long_notional'),
    ('2000','4000','A','open_short_notional'),('-2000','4000','B','open_long_notional')])
def test_actions_and_account_copy(before,size,side,field):
    events=replay_loss_actions(A,[action(before=before,size=size,side=side)],
        curve([-25000000],'perpAllTime'))
    assert len(events)==1
    e=events[0]
    assert Decimal(e['evidence']['actions'][field])==6000000
    material=writer_material(e)['facts']['historical_highlights']
    assert material['account_period_loss']==[{'period':'累计','loss_usd':'25000000'}]
    assert 'asset_trading_pnl_usd' not in material and 'asset_consecutive_losing_rounds' not in material
    assert e['evidence']['test_replay']['attribute_cutoff_ms'] == END
    e['evidence']['address_notes']={A:'模拟地址'}
    draft=DraftBrief(title='总亏损2500万美元的地址操作ETH', content='该地址合约账户累计亏损2500万美元。')
    result=format_news(draft,e)
    assert result.title=='总亏损2500万美元地址操作ETH'
    assert result.content.startswith('据Odaily监测，某地址合约账户（模拟地址）')
    assert format_news(result,e).content.count('模拟地址')==1

def test_finite_period_loss_cannot_qualify_total_loss_direction():
    assert replay_loss_actions(A,[action()],curve([-25000000])) == []


def test_offsetting_same_asset_window_below_net_threshold_is_rejected():
    fills = [action(before='4000',size='1000',side='A'),
             action(before='3000',size='1200',side='B',stamp=END+2)]
    # $3m close and $3.6m open, net only $600k.
    assert replay_loss_actions(A,fills,curve([-25000000],'perpAllTime')) == []


def test_flat_round_trip_and_price_change_do_not_create_net_action():
    fills = [action(size='2000'), action(before='2000',size='2000',side='A',stamp=END+2)]
    fills[-1]['px']='6000'
    assert replay_loss_actions(A,fills,curve([-25000000],'perpAllTime')) == []


def test_closed_position_copy_uses_flat_position_wording():
    e=replay_loss_actions(A,[action(before='2000',size='2000',side='A')],
        curve([-25000000],'perpAllTime'))[0]
    result=format_news(DraftBrief(title='地址平仓ETH多单',content='此次操作使其ETH多单由600万美元降至清仓。'),e)
    assert '降至清仓' not in result.content
    assert '降至空仓' in result.content


def test_offsetting_window_exact_net_threshold_is_accepted():
    fills = [action(before='4000',size='1000',side='A'),
             action(before='3000',size='2000',side='B',stamp=END+2)]
    events = replay_loss_actions(A,fills,curve([-25000000],'perpAllTime'))
    assert len(events) == 1
    assert Decimal(events[0]['evidence']['net_action_notional_usd']) == 3000000


def test_curve_decline_is_never_presented_as_realized_trading_loss():
    payload = curve([10000000,9000000,8000000,7000000])
    assert replay_loss_actions(A, [action()], payload) == []
    giant = curve([-25000000,-26000000,-27000000,-28000000], 'perpAllTime')
    event = replay_loss_actions(A, [action()], giant)[0]
    h = writer_material(event)['facts']['historical_highlights']
    assert 'account_consecutive_loss' not in h
    assert 'account_consecutive_rounds' not in h
    assert 'account_losing_days' not in h
