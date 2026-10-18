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


@cli.command()
def scorecard(config: ConfigOpt = None) -> None:
    """Print the lifetime paper-trading scorecard as JSON (works while the UI runs)."""
    s = _settings(config, Mode.PAPER)

    async def main() -> None:
        from kst.core.bus import EventBus
        from kst.core.clock import SystemClock
        from kst.persistence.db import Database
        from kst.persistence.repo import Repository
        from kst.portfolio.scorecard import paper_scorecard

        clock = SystemClock()
        repo = Repository(Database(s.db_path), EventBus(), clock)
        try:
            await repo.db.init()
            typer.echo(json.dumps(await paper_scorecard(repo, clock.now_ms()), indent=2))
        finally:
            await repo.db.close()

    asyncio.run(main())


@cli.command("replay")
def replay_command(
    recordings: Annotated[
        list[str], typer.Argument(help="Recording paths or quoted globs (.jsonl.gz or .jsonl)")
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="New output directory; never overwrites a previous run"),
    ] = None,
    config: ConfigOpt = None,
    sweep: Annotated[
        list[str] | None,
        typer.Option("--sweep", help="Repeatable parameter grid, e.g. z=0.5,1 or kelly=0.1,0.25"),
    ] = None,
    sensitivity: Annotated[
        bool, typer.Option("--sensitivity", help="Also compare base, 2x and 5x maker queue-ahead assumptions")
    ] = False,
    warmup_seconds: Annotated[
        float | None,
        typer.Option("--warmup-seconds", min=0, help="Ingest inputs without strategy orders during warmup"),
    ] = None,
) -> None:
    """Offline backtest using recorded data, ManualClock and PaperBroker; never contacts the exchange."""
    from uuid import uuid4

    from kst.backtest.recordings import expand_paths
    from kst.backtest.replay import run_replays

    try:
        s = load_settings(*(config or DEFAULT_CONFIG_FILES)).model_copy(deep=True)
        if warmup_seconds is not None:
            s.replay.warmup_s = warmup_seconds
        paths = expand_paths(recordings)
        target = output or s.data_dir / "replays" / f"run-{uuid4().hex[:12]}"
        # Keep trading-loop logs separate from machine-readable stdout.
        configure_logging("WARNING")
        result = asyncio.run(run_replays(s, paths, target, sweep or [], sensitivity))
        typer.echo(json.dumps({"output": str(target.resolve()), **result}, indent=2))
    except (ValueError, OSError) as exc:
        typer.secho(f"Replay failed: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


@cli.command("calibrate")
def calibrate_command(
    inputs: Annotated[
        list[Path] | None, typer.Option("--input", help="Repeatable normalized market CSV/JSONL input")
    ] = None,
    series: Annotated[
        list[str] | None,
        typer.Option("--series", help="Repeatable series ticker for bounded public history download"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o", help="New candidate directory; active calibration is never overwritten"
        ),
    ] = None,
    config: ConfigOpt = None,
    as_of_ms: Annotated[
        int | None, typer.Option("--as-of-ms", min=1, help="Outcome availability cutoff; default is now")
    ] = None,
    max_markets: Annotated[int | None, typer.Option("--max-markets", min=1, max=10000)] = None,
) -> None:
    """Fit and validate market probability calibration; save a candidate and reliability plots."""
    from uuid import uuid4

    from kst.calibration.artifacts import write_job
    from kst.calibration.collect import collect_markets
    from kst.calibration.data import MarketSample, read_samples
    from kst.core.clock import SystemClock
    from kst.kalshi.rest import KalshiRest

    try:
        if bool(inputs) == bool(series):
            raise ValueError("Choose either --input samples or --series history download")
        s = load_settings(*(config or DEFAULT_CONFIG_FILES)).model_copy(deep=True)
        if max_markets is not None:
            s.calibration_tools.max_markets = max_markets
        now = as_of_ms or SystemClock().now_ms()
        target = output or s.data_dir / "calibrations" / f"probability-{uuid4().hex[:12]}"
        if target.exists():
            raise ValueError(f"Output directory already exists: {target}")
        configure_logging("WARNING")
        acquisition, evidence = None, None
        if inputs:
            samples, sources, duplicates = read_samples(inputs, MarketSample)
            acquisition = {"exact_duplicates_removed": duplicates}
        else:

            async def download() -> tuple[list[MarketSample], dict[str, Any], list[dict[str, Any]]]:
                # Public reads only: no signer, key parsing, account or order requests.
                rest = KalshiRest(s.kalshi.rest_url)
                try:
                    return await collect_markets(rest, series or [], s.calibration_tools, now)
                finally:
                    await rest.aclose()

            samples, acquisition, evidence = asyncio.run(download())
            sources = [{"api": s.kalshi.rest_url, "series": series, "retrieved_ms": SystemClock().now_ms()}]
        result = write_job(s, samples, sources, target, now, acquisition=acquisition, evidence=evidence)
        typer.echo(json.dumps(result, indent=2))
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        typer.secho(f"Calibration failed: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


@cli.command("calibrate-weather")
def calibrate_weather_command(
    inputs: Annotated[
        list[Path] | None,
        typer.Option("--input", help="Repeatable paired final-high/ensemble JSONL or CSV input"),
    ] = None,
    recordings: Annotated[
        list[str] | None,
        typer.Option("--recordings", help="Repeatable recording path/glob with pre-day ensembles"),
    ] = None,
    observations: Annotated[
        list[Path] | None,
        typer.Option("--observations", help="Final station/day high CSV/JSONL for archive pairing"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="New candidate directory for weather YAML and diagnostics"),
    ] = None,
    config: ConfigOpt = None,
    as_of_ms: Annotated[int | None, typer.Option("--as-of-ms", min=1)] = None,
) -> None:
    """Fit station bias and ensemble spread against final daily reports; save a candidate YAML."""
    import hashlib
    from uuid import uuid4

    from kst.backtest.recordings import expand_paths
    from kst.calibration.archive import pair_archive
    from kst.calibration.artifacts import write_job
    from kst.calibration.data import WeatherSample, read_samples
    from kst.core.clock import SystemClock

    acquisition: dict[str, Any]
    sources: list[dict[str, Any]]
    try:
        if (inputs and (recordings or observations)) or (not inputs and not (recordings and observations)):
            raise ValueError("Choose --input paired samples OR both --recordings and --observations")
        s = load_settings(*(config or DEFAULT_CONFIG_FILES))
        now = as_of_ms or SystemClock().now_ms()
        target = output or s.data_dir / "calibrations" / f"weather-{uuid4().hex[:12]}"
        if inputs:
            samples, sources, duplicates = read_samples(inputs, WeatherSample)
            acquisition = {"exact_duplicates_removed": duplicates}
        else:
            paths = expand_paths(recordings or [])

            def fingerprints() -> list[dict[str, str]]:
                result = []
                for path in paths:
                    with path.open("rb") as stream:
                        result.append(
                            {"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
                        )
                return result

            sources = fingerprints()
            samples, acquisition = pair_archive(s, paths, observations or [])
            if sources != fingerprints():
                raise ValueError("Recordings changed during pairing; use completed archive files")
            sources.extend(acquisition["observation_sources"])
        configure_logging("WARNING")
        result = write_job(s, samples, sources, target, now, weather=True, acquisition=acquisition)
        typer.echo(json.dumps(result, indent=2))
    except (ValueError, OSError, RuntimeError) as exc:
        typer.secho(f"Weather calibration failed: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc


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
