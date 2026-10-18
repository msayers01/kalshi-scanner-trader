"""Bankroll presets: scaling, hands-off switches, persistence across restarts, runtime apply, auto-resume."""

from __future__ import annotations

import itertools
from pathlib import Path

import httpx
import pytest

from kst.config import Settings, load_settings
from kst.core.types import Actor, BookSide, EngineState
from kst.presets import PRESETS, apply_preset, get_preset
from kst.risk.limits import RiskLimits
from tests.conftest import Harness


def test_starter_reproduces_the_shipped_defaults() -> None:
    defaults = load_settings()  # config/default.yaml, no preset
    starter = get_preset("starter")
    assert defaults.preset is None
    for k, v in starter.limits().items():
        assert getattr(defaults.risk.limits, k) == pytest.approx(v), k
    assert defaults.strategies.structural_arb.max_basket_usd == starter.arb_basket_usd
    assert defaults.risk.cash_buffer_usd == starter.cash_buffer_usd


@pytest.mark.parametrize("key", list(PRESETS))
def test_every_preset_is_valid_and_proportional(key: str) -> None:
    p = PRESETS[key]
    lim = RiskLimits.model_validate({**RiskLimits().model_dump(), **p.limits()})
    b = p.bankroll_usd
    assert lim.max_order_usd == pytest.approx(0.05 * b)
    assert lim.daily_loss_limit_usd == pytest.approx(0.05 * b)
    assert lim.max_drawdown_usd == pytest.approx(0.10 * b)
    assert lim.max_total_exposure_usd == pytest.approx(0.5 * b)
    assert (
        lim.max_exposure_per_market_usd <= lim.max_exposure_per_event_usd <= lim.max_exposure_per_category_usd
    )
    assert lim.daily_loss_limit_usd < lim.max_drawdown_usd  # pause before kill


def test_tiers_grow_monotonically() -> None:
    ps = list(PRESETS.values())
    for a, b in itertools.pairwise(ps):
        assert a.bankroll_usd < b.bankroll_usd
        assert a.markets_per_day <= b.markets_per_day and a.open_positions <= b.open_positions


def test_apply_preset_is_hands_off_but_never_touches_mode() -> None:
    s = Settings()
    apply_preset(s, "Standard")
    assert s.preset == "standard"
    assert s.engine.autotrade and s.engine.auto_resume and s.llm.fallback == "allow"
    assert s.risk.limits.max_order_usd == 250 and s.paper.starting_balance_usd == 5_000
    assert s.app.mode.value == "paper"
    with pytest.raises(ValueError, match="unknown preset"):
        get_preset("whale")


def test_preset_from_yaml(tmp_path: Path) -> None:
    f = tmp_path / "c.yaml"
    f.write_text("preset: micro\nrisk:\n  limits:\n    max_markets_per_day: 99\n", encoding="utf-8")
    s = load_settings(f)
    assert s.preset == "micro" and s.risk.limits.max_order_usd == 5
    assert s.risk.limits.max_markets_per_day == 6  # the preset is applied on top of the YAML


# ------------------------------------------------------------------ runtime (ControlService)


async def test_runtime_apply_persists_limits_and_turns_on_autotrade(hs: Harness) -> None:
    hs.control.autotrade = False
    res = await hs.control.apply_preset("large", Actor.WEB)
    assert res["ok"] and res["autotrade"] and hs.control.autotrade
    assert hs.risk.limits.max_order_usd == 1_250
    assert hs.settings.engine.auto_resume and hs.settings.llm.fallback == "allow"
    assert hs.control.status()["preset"] == "large"


async def test_reset_paper_requires_flat_book(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.40, 50)], asks=[(0.42, 50)])
    from kst.core.fixedpoint import Price, Qty
    from kst.domain.orders import TakeIntent

    await hs.oms.take(TakeIntent("M", BookSide.BID, Price(4200), Qty(500), "t"), "edge_taker")
    res = await hs.control.apply_preset("micro", Actor.WEB, reset_paper=True)
    assert not res["ok"] and "Close all positions" in res["reason"]
    assert hs.risk.limits.max_order_usd != 5  # nothing applied
    await hs.control.close_position("M", Actor.WEB)
    res = await hs.control.apply_preset("micro", Actor.WEB, reset_paper=True)
    assert res["ok"]
    s = hs.portfolio.summary()
    assert s.cash == 100_000_000 and s.equity == 100_000_000 and s.pnl_today == 0


async def test_scan_only_never_turns_autotrade_on(hs: Harness) -> None:
    hs.control.scan_only = True
    hs.control.autotrade = False
    res = await hs.control.apply_preset("small", Actor.WEB)
    assert res["ok"] and not res["autotrade"] and not hs.control.autotrade


# ------------------------------------------------------------------ auto-resume


async def test_auto_resume_only_undoes_system_pauses_on_a_later_day(hs: Harness) -> None:
    hs.settings.engine.auto_resume = True
    await hs.control.pause(Actor.SYSTEM, "daily_loss: test")
    assert not await hs.control.auto_resume_tick()  # same trading day: stay paused
    hs.clock.set(hs.clock.now_ms() + 86_400_000)
    assert await hs.control.auto_resume_tick()
    assert hs.state.get() is EngineState.RUNNING

    await hs.control.pause(Actor.WEB, "operator")
    hs.clock.set(hs.clock.now_ms() + 86_400_000)
    assert not await hs.control.auto_resume_tick()  # a human's pause is never undone
    assert hs.state.get() is EngineState.PAUSED

    await hs.control.kill(Actor.SYSTEM, "drawdown")
    hs.clock.set(hs.clock.now_ms() + 86_400_000)
    assert not await hs.control.auto_resume_tick()  # KILL needs a human
    assert hs.state.get() is EngineState.KILLED


async def test_auto_resume_off_by_default(hs: Harness) -> None:
    assert not hs.settings.engine.auto_resume
    await hs.control.pause(Actor.SYSTEM, "daily_loss")
    hs.clock.set(hs.clock.now_ms() + 86_400_000)
    assert not await hs.control.auto_resume_tick()


# ------------------------------------------------------------------ restart + API


async def test_config_preset_wins_and_ui_choice_survives_restart(tmp_path: Path) -> None:
    from kst.app import Application

    def make(preset: str | None) -> Application:
        s = Settings()
        s.app.data_dir = tmp_path
        s.llm.mode = "off"
        if preset:
            apply_preset(s, preset)
        return Application(s)

    a = make(None)
    await a._restore()
    await a.control.apply_preset("standard", Actor.WEB)
    await a.control.set_limits({"max_markets_per_day": 3}, Actor.WEB)  # a tweak on top of the preset
    await a.repo.flush()
    await a.repo.stop()

    b = make(None)  # no preset in config: the UI choice + tweak come back
    await b._restore()
    assert b.settings.preset == "standard" and b.control.autotrade and b.settings.llm.fallback == "allow"
    assert b.risk.limits.max_order_usd == 250 and b.risk.limits.max_markets_per_day == 3
    await b.repo.stop()

    c = make("micro")  # a different preset in config: its limits win, the old tweak is dropped
    await c._restore()
    assert c.risk.limits.max_order_usd == 5 and c.risk.limits.max_markets_per_day == 6
    await c.repo.stop()


async def test_presets_api(tmp_path: Path) -> None:
    from kst.api.app import create_app
    from kst.app import Application

    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    s.web.serve_ui = False
    app = Application(s)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test"
    ) as c:
        d = (await c.get("/api/presets")).json()
        assert d["active"] is None and [p["key"] for p in d["presets"]] == list(PRESETS)
        r = (await c.post("/api/presets/small/apply", json={})).json()
        assert r["ok"] and app.risk.limits.max_order_usd == 25
        assert (await c.get("/api/presets")).json()["active"] == "small"
        assert (await c.post("/api/presets/whale/apply", json={})).status_code == 422
        evil = await c.post("/api/presets/pro/apply", json={}, headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
