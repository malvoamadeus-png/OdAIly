from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packages.jin10_monitor.models import Jin10Item
from packages.macro_newsflash.jin10_release import release_updates_from_items
from packages.macro_newsflash.models import MacroUpdate
from packages.macro_newsflash.schedule import (
    ScheduledRelease,
    _parse_fed_calendar,
    _tradingview_mapping,
)
from packages.macro_newsflash.settings import MacroNewsflashSettings
from packages.macro_newsflash.store import MacroNewsflashStore
from packages.macro_newsflash.worker import MacroNewsflashWorker


def _settings(db_path: Path) -> MacroNewsflashSettings:
    return MacroNewsflashSettings(
        database_path=db_path,
        push_endpoint="http://127.0.0.1:9999/push/data",
        poll_interval_seconds=30,
        release_poll_interval_seconds=1.0,
        release_request_timeout_seconds=3.0,
        quiet_poll_interval_seconds=900,
        request_timeout_seconds=5.0,
        push_timeout_seconds=5.0,
        bea_schedule_url="https://www.bea.gov/news/schedule",
        fed_calendar_url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        tradingview_calendar_url="https://economic-calendar.tradingview.com/events",
    )


def _item(item_id: str, content: str, published_at: datetime) -> Jin10Item:
    return Jin10Item(
        source_item_id=item_id,
        title=content,
        content=content,
        source_url=f"https://flash.jin10.com/detail/{item_id}",
        published_at=published_at,
    )


def test_jin10_parses_employment_cpi_retail_and_fomc():
    scheduled = datetime(2026, 12, 4, 13, 30, tzinfo=UTC)
    emp_release = {
        "release_id": "bls_employment:2026-12-04",
        "scheduled_at": scheduled.isoformat(),
        "metric_ids": ["nonfarm_payrolls", "unemployment_rate"],
    }
    emp_items = [
        _item("adp", "美国11月ADP就业人数 14.6万人，预期15万人，前值23.3万人", scheduled + timedelta(seconds=2)),
        _item("u6", "美国11月U6失业率 7.7%，前值7.7%", scheduled + timedelta(seconds=2)),
        _item("nfp", "美国11月季调后非农就业人口 22.7万人，预期20万人，前值由1.2万人修正为3.6万人", scheduled + timedelta(seconds=3)),
        _item("unrate", "美国11月失业率 4.2%，预期4.20%，前值4.1%", scheduled + timedelta(seconds=4)),
    ]
    emp_updates = release_updates_from_items(emp_release, emp_items, observed_at=scheduled + timedelta(seconds=5))
    by_id = {u.metric_id: u for u in emp_updates}
    assert set(by_id) == {"nonfarm_payrolls", "unemployment_rate"}
    assert by_id["nonfarm_payrolls"].actual == "22.7万人"
    assert by_id["nonfarm_payrolls"].previous == "3.6万人"
    assert by_id["nonfarm_payrolls"].expected == "20万人"
    assert by_id["nonfarm_payrolls"].event_key == "jin10:bls_employment:2026-12-04:nonfarm_payrolls"
    assert by_id["unemployment_rate"].actual == "4.2%"
    assert by_id["unemployment_rate"].event_key == "jin10:bls_employment:2026-12-04:unemployment_rate"

    cpi_release = {
        "release_id": "bls_cpi:2026-12-10",
        "scheduled_at": scheduled.isoformat(),
        "metric_ids": ["cpi_yoy_nsa", "cpi_mom_nsa", "cpi_mom_sa", "core_cpi_yoy_nsa", "core_cpi_mom_sa"],
    }
    cpi_items = [
        _item("c1", "美国11月未季调CPI年率 2.7%，预期2.70%，前值2.6%", scheduled + timedelta(seconds=1)),
        _item("c2", "美国11月季调后CPI月率 0.3%，预期0.30%，前值0.2%", scheduled + timedelta(seconds=1)),
        _item("c3", "美国11月未季调核心CPI年率 3.3%，预期3.30%，前值3.3%", scheduled + timedelta(seconds=2)),
        _item("c4", "美国11月季调后核心CPI月率 0.3%，预期0.30%，前值0.3%", scheduled + timedelta(seconds=2)),
    ]
    cpi_updates = release_updates_from_items(cpi_release, cpi_items, observed_at=scheduled + timedelta(seconds=3))
    assert {u.metric_id for u in cpi_updates} == {"cpi_yoy_nsa", "cpi_mom_sa", "core_cpi_yoy_nsa", "core_cpi_mom_sa"}

    retail_release = {
        "release_id": "census_retail_sales:2026-12-16",
        "scheduled_at": scheduled.isoformat(),
        "metric_ids": ["retail_sales_mom"],
    }
    retail_items = [
        _item("r_core", "美国11月核心零售销售月率 0.2%，预期0.40%，前值0.1%", scheduled + timedelta(seconds=1)),
        _item("r_main", "美国11月零售销售月率 0.7%，预期0.50%，前值由0.4%修正为0.5%", scheduled + timedelta(seconds=2)),
    ]
    retail_updates = release_updates_from_items(retail_release, retail_items, observed_at=scheduled + timedelta(seconds=3))
    assert len(retail_updates) == 1
    assert retail_updates[0].actual == "0.7%"
    assert retail_updates[0].previous == "0.5%"
    assert retail_updates[0].event_key == "jin10:census_retail_sales:2026-12-16"

    fomc_release = {
        "release_id": "fomc:2026-12-09",
        "scheduled_at": scheduled.isoformat(),
        "metric_ids": ["fomc_rate"],
    }
    fomc_items = [
        _item("f_lower", "美国至12月9日美联储利率决定(下限) 3.50%，预期3.50%，前值3.75%", scheduled + timedelta(seconds=1)),
        _item("f_upper", "美国至12月9日美联储利率决定(上限) 3.75%，预期3.75%，前值4.00%", scheduled + timedelta(seconds=1)),
        _item("f_stmt", "美联储将联邦基金利率目标区间下调25个基点至3.50%-3.75%", scheduled + timedelta(seconds=2)),
    ]
    fomc_updates = release_updates_from_items(fomc_release, fomc_items, observed_at=scheduled + timedelta(seconds=3))
    assert len(fomc_updates) == 1
    assert fomc_updates[0].rate_action == "下调"
    assert fomc_updates[0].actual == "3.50%至3.75%"
    assert "下调联邦基金利率目标区间至3.50%至3.75%" in fomc_updates[0].headline()

    # Commentary articles and absent expected values must behave cleanly
    pce_release = {
        "release_id": "bea_pce:2026-11-25",
        "scheduled_at": scheduled.isoformat(),
        "metric_ids": ["pce_yoy"],
    }
    pce_commentary = [
        _item("c_pce", "【美国PCE高于预期】金十数据11月25日讯，美国10月PCE物价指数年率为3.8%。", scheduled + timedelta(seconds=5)),
        _item("q_pce", "美国第三季度PCE物价指数年化季率修正值 2.1%，前值2.1%。", scheduled + timedelta(seconds=6)),
    ]
    assert release_updates_from_items(pce_release, pce_commentary, observed_at=scheduled + timedelta(seconds=7)) == []

    no_exp_retail = release_updates_from_items(
        retail_release,
        [_item("r_no_exp", "美国11月零售销售月率 0.7%，前值0.5%。", scheduled + timedelta(seconds=2))],
        observed_at=scheduled + timedelta(seconds=3),
    )
    assert len(no_exp_retail) == 1
    assert no_exp_retail[0].expected == ""
    assert "预期" not in no_exp_retail[0].headline()


def test_fed_panel_and_tradingview_mappings():
    html = """
    <div class="panel">
      <div class="panel-heading">2026 FOMC Meetings</div>
      <div class="fomc-meeting"><div class="fomc-meeting__date">December 8-9*</div></div>
    </div>
    <div class="panel">
      <div class="panel-heading">2027 FOMC Meetings</div>
      <div class="fomc-meeting"><div class="fomc-meeting__date">Jan/Feb 31-1</div></div>
    </div>
    """
    parsed = _parse_fed_calendar(html, "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", now=datetime(2026, 11, 25, 14, 0, tzinfo=UTC))
    assert [item.release_id for item in parsed] == ["fomc:2026-12-09", "fomc:2027-02-01"]
    assert _tradingview_mapping("Non Farm Payrolls", "Bureau of Labour Statistics")[0] == "bls_employment"
    assert _tradingview_mapping("Inflation Rate YoY", "Bureau of Labour Statistics")[0] == "bls_cpi"
    assert _tradingview_mapping("Retail Sales MoM", "Census Bureau")[0] == "census_retail_sales"
    assert _tradingview_mapping("Initial Jobless Claims", "Department of Labour")[0] == "dol_initial_claims"
    assert _tradingview_mapping("PCE Price Index YoY", "Bureau of Economics Analysis")[0] == "bea_pce"
    assert _tradingview_mapping("Interest Rate", "Federal Reserve")[0] == "fomc"


def test_worker_quiet_skips_jin10_and_stops_immediately_after_publish(tmp_path, monkeypatch):
    store = MacroNewsflashStore(tmp_path / "macro.sqlite")
    store.initialize()
    store.record_source_refresh(checked_sources={"bls"}, errors={"bls": "stale error"})
    store.prune_source_errors(allowed_sources={"jin10_macro"})
    assert store.dashboard()["source_errors"] == {}

    now = datetime.now(UTC)
    upcoming_at = now + timedelta(seconds=30)
    release = ScheduledRelease(
        release_id="bls_employment:2026-12-04",
        source_name="TradingView · BLS",
        title="Employment Situation",
        metric_ids=("nonfarm_payrolls", "unemployment_rate"),
        scheduled_at=upcoming_at.isoformat(timespec="seconds"),
        source_url="https://www.bls.gov/",
    )
    monkeypatch.setattr(
        "packages.macro_newsflash.worker.fetch_release_schedule",
        lambda settings, now=None: ([release], {}, {"TradingView · BLS"}),
    )
    jin10_calls = 0

    def fake_fetch_jin10(**kwargs):
        nonlocal jin10_calls
        jin10_calls += 1
        return [
            _item("nfp1", "美国11月季调后非农就业人口 21万人，预期20万人，前值18万人", upcoming_at + timedelta(seconds=1)),
            _item("un1", "美国11月失业率 4.1%，预期4.2%，前值4.2%", upcoming_at + timedelta(seconds=2)),
        ]

    monkeypatch.setattr("packages.macro_newsflash.worker.fetch_jin10_items", fake_fetch_jin10)
    pushed: list[str] = []

    class FakePush:
        def push(self, **kwargs):
            pushed.append(kwargs["idempotency_key"])
            return SimpleNamespace(ok=True, error=None)

    worker = MacroNewsflashWorker(settings=_settings(tmp_path / "macro.sqlite"), store=store, push_client=FakePush())

    # Outside [t-10s, t+60s]: quiet mode refreshes calendar only and never calls Jin10
    quiet_result = worker.run_once()
    assert quiet_result["mode"] == "quiet"
    assert quiet_result["calendar_refreshed"] is True
    assert jin10_calls == 0
    # Next wakeup should be ~20s (10s before release), not 900s
    assert 15 <= worker._poll_interval(now) <= 25

    # Move release into active window [t-10s, t+60s]
    active_at = datetime.now(UTC)
    active_release = ScheduledRelease(
        release_id="bls_employment:2026-12-04",
        source_name="TradingView · BLS",
        title="Employment Situation",
        metric_ids=("nonfarm_payrolls", "unemployment_rate"),
        scheduled_at=active_at.isoformat(timespec="seconds"),
        source_url="https://www.bls.gov/",
    )
    store.replace_release_schedule([active_release], refreshed_sources={"TradingView · BLS"})
    monkeypatch.setattr(
        "packages.macro_newsflash.worker.fetch_jin10_items",
        lambda **kwargs: [
            _item("nfp1", "美国11月季调后非农就业人口 21万人，预期20万人，前值18万人", active_at + timedelta(seconds=1)),
            _item("un1", "美国11月失业率 4.1%，预期4.2%，前值4.2%", active_at + timedelta(seconds=2)),
        ],
    )
    win_result = worker.run_once()
    assert win_result["mode"] == "window"
    assert win_result["published"] == 2
    assert len(pushed) == 2

    # Immediately after both metrics publish, worker returns to quiet sleep (~900s) and skips Jin10
    assert worker._active_jin10_releases(store.upcoming_releases(), datetime.now(UTC)) == []
    assert worker._poll_interval(datetime.now(UTC)) >= 890
