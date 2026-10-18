"""kst command-line interface (Typer)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from kst.config import DEFAULT_CONFIG_FILES, Settings, load_settings
from kst.core.logging import configure_logging
from kst.core.types import Mode

cli = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Kalshi scanner & quantitative auto-trader."
)

ConfigOpt = Annotated[
    list[Path] | None,
    typer.Option(
        "--config", "-c", help="YAML config file(s); default config/default.yaml + config/local.yaml"
    ),
]
ModeOpt = Annotated[Mode | None, typer.Option("--mode", help="paper | demo | live (overrides config)")]


def _settings(config: list[Path] | None, mode: Mode | None) -> Settings:
    s = load_settings(*(config or DEFAULT_CONFIG_FILES))
    if mode is not None:
        s.app.mode = mode
        if mode is Mode.DEMO:
            s = Settings.model_validate(s.model_dump())  # re-derive demo hosts
    configure_logging(s.app.log_level, s.data_dir / "logs")
    return s


def _run(settings: Settings, *, live_armed: bool, scan_only: bool) -> None:
    from kst.app import Application, run_application

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows: no add_signal_handler
                loop.add_signal_handler(sig, stop.set)
        app = Application(settings, live_armed=live_armed, scan_only=scan_only)
        with contextlib.suppress(asyncio.CancelledError):
            await run_application(app, stop)

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


@cli.command()
def run(
    config: ConfigOpt = None,
    mode: ModeOpt = None,
    autotrade: Annotated[
        bool | None, typer.Option("--autotrade/--no-autotrade", help="Override engine.autotrade")
    ] = None,
    i_understand_live_trading: Annotated[
        bool, typer.Option("--i-understand-live-trading", help="Required for live mode")
    ] = False,
    no_web: Annotated[bool, typer.Option("--no-web")] = False,
    no_discord: Annotated[bool, typer.Option("--no-discord")] = False,
) -> None:
    """Run the full system: market data, scanner, LLM gate, auto-trader, web UI, Discord."""
    s = _settings(config, mode)
    if s.app.mode is Mode.LIVE and not i_understand_live_trading:
        typer.secho(
            "Live mode trades real money. Re-run with --i-understand-live-trading.", fg="red", err=True
        )
        raise typer.Exit(2)
    if autotrade is not None:
        s.engine.autotrade = autotrade
    if no_web:
        s.web.enabled = False
    if no_discord:
        s.discord.enabled = False
    _banner(s)
    _run(s, live_armed=i_understand_live_trading, scan_only=False)


@cli.command()
def scan(config: ConfigOpt = None, no_discord: Annotated[bool, typer.Option("--no-discord")] = False) -> None:
    """Scanner only (web UI + Discord + manual trading; no automatic orders)."""
    s = _settings(config, None)
    s.engine.autotrade = False
    if no_discord:
        s.discord.enabled = False
    _banner(s, scan_only=True)
    _run(s, live_armed=False, scan_only=True)


@cli.command("llm-packet")
def llm_packet(
    config: ConfigOpt = None,
    top: Annotated[int, typer.Option(help="rows")] = 20,
    warmup: Annotated[float, typer.Option(help="seconds of live data before printing")] = 25.0,
) -> None:
    """Print a compact decision packet (paste into ChatGPT/Claude)."""
    s = _settings(config, Mode.PAPER)
    s.web.enabled = False
    s.discord.enabled = False
    s.llm.mode = "off"

    from kst.app import Application

    async def main() -> str:
        app = Application(s, scan_only=True)
        await app.start()
        try:
            await asyncio.sleep(warmup)
            return app.export_llm_packet(top)
        finally:
            await app.stop()

    print(asyncio.run(main()))


@cli.command()
def check(config: ConfigOpt = None, mode: ModeOpt = None) -> None:
    """Validate config, credentials, connectivity, and LLM availability."""
    s = _settings(config, mode)

    async def main() -> int:
        from kst.app import build_signer
        from kst.kalshi.rest import KalshiRest
        from kst.llm.providers import build_provider

        problems = 0
        typer.echo(f"mode={s.app.mode.value}  db={s.db_path}  rest={s.kalshi.rest_url}")
        signer = None
        try:
            signer = build_signer(s)
            typer.echo(
                f"credentials: {'yes (' + signer.key_type + ')' if signer else 'none → REST polling, paper only'}"
            )
        except Exception as exc:
            problems += 1
            typer.secho(f"credentials: FAILED to load key: {exc}", fg="red")
        rest = KalshiRest(s.kalshi.rest_url, signer=signer)
        try:
            st = await rest.exchange_status()
            typer.echo(
                f"exchange: trading_active={st.get('trading_active')} exchange_active={st.get('exchange_active')}"
            )
            if signer:
                bal = await rest.balance()
                typer.echo(f"balance: ${int(bal.get('balance', 0)) / 100:,.2f}")
                lim = await rest.account_limits()
                typer.echo(
                    f"api tier: {lim.get('usage_tier')} read={lim.get('read')} write={lim.get('write')}"
                )
        except Exception as exc:
            problems += 1
            typer.secho(f"kalshi: {exc}", fg="red")
        finally:
            await rest.aclose()
        if s.llm.mode != "off":
            p = build_provider(s.llm.provider)
            ok = await p.available()
            typer.echo(f"llm: {s.llm.mode} via {p.name}:{p.model} reachable={ok}")
            if not ok:
                typer.secho("  (gate falls back to llm.fallback=" + s.llm.fallback + ")", fg="yellow")
        typer.echo(
            f"discord: enabled={s.discord.enabled} token={'set' if s.discord.token else 'missing'} channel={s.discord.channel_id} users={len(s.discord.allowed_user_ids)}"
        )
        if s.discord.enabled and not s.discord.allowed_user_ids:
            problems += 1
            typer.secho("discord: allowed_user_ids is empty — nobody could use the controls", fg="red")
        typer.echo(
            f"autotrade={s.engine.autotrade}  limits: {s.risk.limits.max_markets_per_day} markets/day, daily loss ${s.risk.limits.daily_loss_limit_usd}"
        )
        return problems

    raise typer.Exit(1 if asyncio.run(main()) else 0)


def _remote(
    url: str, method: str, path: str, token: str | None, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = httpx.request(method, url.rstrip("/") + path, json=body, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


UrlOpt = Annotated[str, typer.Option(help="running kst web API")]
TokenOpt = Annotated[str | None, typer.Option(envvar="KST_WEB__API_TOKEN", help="API token")]


@cli.command()
def status(url: UrlOpt = "http://127.0.0.1:8787", token: TokenOpt = None) -> None:
    """Status of a running instance."""
    typer.echo(json.dumps(_remote(url, "GET", "/api/status", token), indent=2))


@cli.command()
def kill(
    url: UrlOpt = "http://127.0.0.1:8787",
    token: TokenOpt = None,
    flatten: Annotated[bool, typer.Option("--flatten", help="also exit all positions")] = False,
) -> None:
    """Kill switch for a running instance (cancel all orders, stop trading)."""
    typer.echo(
        json.dumps(
            _remote(url, "POST", "/api/control/kill", token, {"reason": "cli", "flatten": flatten}), indent=2
        )
    )


@cli.command()
def pause(url: UrlOpt = "http://127.0.0.1:8787", token: TokenOpt = None) -> None:
    """Pause new entries on a running instance."""
    typer.echo(json.dumps(_remote(url, "POST", "/api/control/pause", token, {"reason": "cli"}), indent=2))


@cli.command()
def resume(url: UrlOpt = "http://127.0.0.1:8787", token: TokenOpt = None) -> None:
    """Resume a running instance."""
    typer.echo(json.dumps(_remote(url, "POST", "/api/control/resume", token), indent=2))


@cli.command()
def report(config: ConfigOpt = None, mode: ModeOpt = None, days: int = 14) -> None:
    """Daily P&L history from the database."""
    s = _settings(config, mode)

    async def main() -> None:
        from kst.core.bus import EventBus
        from kst.core.clock import SystemClock
        from kst.persistence.db import Database
        from kst.persistence.repo import Repository

        repo = Repository(Database(s.db_path), EventBus(), SystemClock())
        await repo.start()
        rows = await repo.daily_pnl(days)
        if not rows:
            typer.echo("no daily reports yet")
        for r in rows:
            wr = f"{r.wins / (r.wins + r.losses):.0%}" if r.wins + r.losses else "—"
            typer.echo(
                f"{r.day}  net ${r.net_usd:>9.2f}  realized ${r.realized_usd:>9.2f}  fees ${r.fees_usd:>7.2f}  trades {r.trades:>4}  win {wr:>4}  equity ${r.end_equity_usd:,.2f}"
            )
        await repo.stop()

    asyncio.run(main())


def _banner(s: Settings, scan_only: bool = False) -> None:
    color = {"paper": "cyan", "demo": "yellow", "live": "red"}.get(s.app.mode.value, "white")
    typer.secho(
        f"kst · mode={s.app.mode.value.upper()} · autotrade={'off (scan only)' if scan_only else s.engine.autotrade}",
        fg=color,
        bold=True,
    )
    if s.web.enabled:
        typer.echo(f"web UI → http://{s.web.host}:{s.web.port}")


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
