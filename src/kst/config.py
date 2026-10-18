"""Typed settings tree.

Precedence (highest first): environment variables (`KST_` prefix, `__` nesting, e.g.
`KST_KALSHI__KEY_ID`) → `.env` → `config/local.yaml` → `config/default.yaml`.
Every tunable that affects P&L or risk carries a comment explaining its effect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from kst.core.types import Mode
from kst.risk.limits import RiskLimits

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILES = (REPO_ROOT / "config" / "default.yaml", REPO_ROOT / "config" / "local.yaml")

_HOSTS = {
    "prod": (
        "https://external-api.kalshi.com/trade-api/v2",
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    ),
    "demo": (
        "https://external-api.demo.kalshi.co/trade-api/v2",
        "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
    ),
}


class AppCfg(BaseModel):
    mode: Mode = Mode.PAPER
    data_dir: Path = Path("data")
    trading_day_tz: str = "America/New_York"  # daily limits & daily P&L roll over at midnight here
    log_level: str = "INFO"


class KalshiCfg(BaseModel):
    env: Literal["prod", "demo"] = "prod"  # market data env; demo mode forces "demo"
    rest_url: str = ""  # derived from env when empty
    ws_url: str = ""
    key_id: SecretStr | None = None  # env: KST_KALSHI__KEY_ID
    private_key_path: Path | None = None  # env: KST_KALSHI__PRIVATE_KEY_PATH (PEM, RSA or Ed25519)
    private_key_pem: SecretStr | None = None  # alternative to the path (env: KST_KALSHI__PRIVATE_KEY_PEM)
    # Token-bucket budgets (tokens/s). Basic tier: read 200, write 100. Read actual values via `kst check`.
    read_tokens_per_s: float = 200
    write_tokens_per_s: float = 100
    read_bucket_seconds: float = 3.0
    write_bucket_seconds: float = 1.0
    default_cost: int = 10
    cancel_cost: int = 2
    request_timeout_s: float = 10.0
    subaccount: int = 0

    @model_validator(mode="after")
    def _derive_urls(self) -> KalshiCfg:
        rest, ws = _HOSTS[self.env]
        self.rest_url = self.rest_url or rest
        self.ws_url = self.ws_url or ws
        return self

    @property
    def has_credentials(self) -> bool:
        return self.key_id is not None and (
            self.private_key_path is not None or self.private_key_pem is not None
        )


class UniverseCfg(BaseModel):
    """Which markets the scanner considers. Narrower universe = less noise and fewer API calls."""

    categories: list[str] = Field(default_factory=list)  # empty = all categories
    exclude_categories: list[str] = Field(default_factory=lambda: ["Mentions"])
    include_series: list[str] = Field(default_factory=list)  # if set, ONLY these series
    exclude_series: list[str] = Field(default_factory=list)
    min_volume_24h: float = 0.0  # contracts
    max_hours_to_close: float = 24  # horizon: markets closing later are not fetched (24 h ≈ 5k markets, 9 MB)
    min_seconds_to_close: int = 60
    catalog_refresh_s: float = 300.0  # WS mode (ticker channel keeps stats fresh in between)
    page_delay_s: float = 1.0  # spacing between heavy /markets pages (unauthenticated limits are tight)
    series_refresh_s: float = 6 * 3600.0


class MarketDataCfg(BaseModel):
    source: Literal["auto", "ws", "poll"] = "auto"  # auto = ws if credentials else REST polling
    focus_size: int = 40  # markets with full depth (orderbook) subscriptions
    book_depth: int = 25
    poll_markets_interval_s: float = 60.0  # REST mode: catalog+stats refresh cadence for the universe
    poll_books_interval_s: float = 3.0  # REST: refresh focus-set books (100 per request)
    # REST: extra refresh of only the books open in the UI (≤ 5), so manual tickets price off a book at
    # most this old. One request per interval; 0 disables. Not used with the WS (books stream there).
    ui_book_interval_s: float = 1.0
    ws_subscribe_ticker_all: bool = True
    record: bool = False  # write market data to data/recordings for replay


class CryptoFeedCfg(BaseModel):
    enabled: bool = True
    source: Literal["auto", "kalshi_cf", "coinbase"] = (
        "auto"  # auto = Kalshi CF channel if keys, else Coinbase
    )
    # series prefix → CF Benchmarks index id (settlement index)
    series_index: dict[str, str] = Field(
        default_factory=lambda: {
            "KXBTCD": "BRTI",
            "KXBTC": "BRTI",
            "KXBTC15M": "BRTI",
            "KXETHD": "ETHUSD_RTI",
            "KXETH": "ETHUSD_RTI",
            "KXETH15M": "ETHUSD_RTI",
            "KXSOLD": "SOLUSD_RTI",
            "KXSOL": "SOLUSD_RTI",
            "KXSOL15M": "SOLUSD_RTI",
            "KXXRPD": "XRPUSD_RTI",
            "KXXRP": "XRPUSD_RTI",
            "KXXRP15M": "XRPUSD_RTI",
            "KXDOGED": "DOGEUSD_RTI",
            "KXDOGE": "DOGEUSD_RTI",
        }
    )
    # CF index id → Coinbase product (fallback / vol seeding)
    coinbase_products: dict[str, str] = Field(
        default_factory=lambda: {
            "BRTI": "BTC-USD",
            "ETHUSD_RTI": "ETH-USD",
            "SOLUSD_RTI": "SOL-USD",
            "XRPUSD_RTI": "XRP-USD",
            "DOGEUSD_RTI": "DOGE-USD",
        }
    )
    seed_vol_hours: float = 6.0


class StationCfg(BaseModel):
    name: str
    lat: float
    lon: float
    nws_station: str  # e.g. KNYC — observations for intraday max conditioning
    tz: str  # local timezone (climate day is local *standard* time)
    bias_f: float | None = None  # overrides pricing.weather.bias_f
    spread_inflation: float | None = Field(default=None, gt=0)  # fitted station ensemble spread override


class WeatherFeedCfg(BaseModel):
    enabled: bool = True
    refresh_s: float = 1800.0  # ensemble refresh (models update ~every 6 h; obs every ~5-60 min)
    obs_refresh_s: float = 300.0
    models: list[str] = Field(
        default_factory=lambda: ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_global"]
    )
    user_agent: str = "kst-weather (contact: set KST_FEEDS__WEATHER__USER_AGENT)"
    stations: dict[str, StationCfg] = Field(
        default_factory=lambda: {
            "KXHIGHNY": StationCfg(
                name="NYC Central Park", lat=40.7789, lon=-73.9692, nws_station="KNYC", tz="America/New_York"
            ),
            "KXHIGHCHI": StationCfg(
                name="Chicago Midway", lat=41.7868, lon=-87.7522, nws_station="KMDW", tz="America/Chicago"
            ),
            "KXHIGHMIA": StationCfg(
                name="Miami Intl", lat=25.7881, lon=-80.3169, nws_station="KMIA", tz="America/New_York"
            ),
            "KXHIGHAUS": StationCfg(
                name="Austin Bergstrom", lat=30.1945, lon=-97.6699, nws_station="KAUS", tz="America/Chicago"
            ),
            "KXHIGHLAX": StationCfg(
                name="Los Angeles Intl",
                lat=33.9382,
                lon=-118.3866,
                nws_station="KLAX",
                tz="America/Los_Angeles",
            ),
            "KXHIGHDEN": StationCfg(
                name="Denver Intl", lat=39.8466, lon=-104.6562, nws_station="KDEN", tz="America/Denver"
            ),
            "KXHIGHPHIL": StationCfg(
                name="Philadelphia Intl", lat=39.8733, lon=-75.2268, nws_station="KPHL", tz="America/New_York"
            ),
        }
    )


class FeedsCfg(BaseModel):
    crypto: CryptoFeedCfg = CryptoFeedCfg()
    weather: WeatherFeedCfg = WeatherFeedCfg()


class CryptoPricingCfg(BaseModel):
    distribution: Literal["normal", "student_t"] = "student_t"  # fat tails price cheap OTM contracts better
    nu: float = 5.0  # minimum Student-t ν (fattest tails, at short horizons); must be > 4
    excess_kurtosis_1h: float = 3.0  # 1-hour log-return excess kurtosis; tails thin as 1/τ (see horizon_nu)
    averaging_window_s: float = 60.0  # settlement = mean of last 60 1-s RTI prints
    vol_sample_s: float = 5.0
    fast_half_life_s: float = 300.0
    slow_half_life_s: float = 3600.0
    fast_weight: float = 0.5
    min_vol_annual: float = 0.15  # clamps protect against dead/garbage feeds
    max_vol_annual: float = 4.0
    max_ref_age_s: float = 10.0  # model unavailable if reference price older than this
    implied_weight: float = 0.7  # weight on the event-ladder implied σ vs realized σ (relative-value pricing)
    max_fit_spread: float = 0.06  # only quotes tighter than this enter the implied-σ fit
    min_samples: int = 30


class WeatherPricingCfg(BaseModel):
    bias_f: float = 0.5  # hourly 2 m model maxima under-read the true daily peak
    spread_inflation: float = 1.3  # ensemble under-dispersion correction
    min_bandwidth_f: float = 1.0
    model_floor: float = 0.02  # extra probability std (model error)
    max_forecast_age_s: float = 4 * 3600.0


class CalibrationCfg(BaseModel):
    path: Path = Path("data/calibration.json")  # reviewed `kst calibrate` candidate; identity if missing


class CalibrationToolsCfg(BaseModel):
    model_config = {"allow_inf_nan": False}
    horizon_h: float = Field(default=1, gt=0)  # price snapshot horizon before close
    max_quote_age_s: float = Field(default=300, gt=0)  # reject candles too far before the target horizon
    max_spread: float = Field(default=0.20, gt=0, lt=1)  # exclude illiquid historical quote snapshots
    holdout_fraction: float = Field(default=0.2, gt=0, lt=0.5)  # newest events/dates reserved for validation
    min_train_samples: int = Field(default=200, ge=10)  # prevent unstable two-parameter market fits
    min_holdout_samples: int = Field(default=50, ge=5)
    min_train_events: int = Field(default=20, ge=3)  # related strikes stay in the same split
    min_holdout_events: int = Field(default=10, ge=2)
    l2: float = Field(default=0.01, gt=0)  # shrink toward the identity prior
    max_markets: int = Field(default=1000, ge=1, le=10000)  # bounded public history acquisition
    weather_min_train_days: int = Field(default=60, ge=10)
    weather_min_holdout_days: int = Field(default=20, ge=5)
    weather_min_members: int = Field(default=10, ge=2)  # complete members required for daily-high fit
    weather_max_lead_h: float = Field(default=24, gt=0)  # use forecasts available before the climate day
    weather_max_bias_f: float = Field(default=10, gt=0)  # bounded fit to expose station/source errors
    weather_min_inflation: float = Field(default=0.25, gt=0)
    weather_max_inflation: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def inflation_bounds(self) -> CalibrationToolsCfg:
        if self.weather_min_inflation >= self.weather_max_inflation:
            raise ValueError("weather_min_inflation must be below weather_max_inflation")
        return self


class PricingCfg(BaseModel):
    s_floor: float = 0.15  # min model std in logit units — no model is perfect
    s_p_base: float = 0.35  # market noise in logit units — lower = trust market more (fewer trades)
    crypto: CryptoPricingCfg = CryptoPricingCfg()
    weather: WeatherPricingCfg = WeatherPricingCfg()
    calibration: CalibrationCfg = CalibrationCfg()


class ScannerCfg(BaseModel):
    interval_s: float = 1.0
    top_n: int = 150  # rows kept in the snapshot (UI shows these)
    min_edge_signal: float = 0.02  # $ per contract to flag EDGE
    wide_spread: float = 0.06  # flag WIDE (MM opportunity) when spread ≥ this
    mover_threshold: float = 0.10  # |24h change| to flag MOVER
    # ranking weights (score = Σ w·feature, see scanner/service.py)
    w_edge: float = 10.0
    w_liquidity: float = 1.0
    w_spread: float = 2.0
    w_arb: float = 50.0


class StructuralArbCfg(BaseModel):
    enabled: bool = True
    margin: float = 0.005  # extra $ per basket beyond fees (covers rounding/leg risk)
    max_basket_usd: float = 50.0
    exhaustive_allowlist: list[str] = Field(default_factory=list)  # event tickers asserted exhaustive


class EdgeTakerCfg(BaseModel):
    enabled: bool = True
    min_edge: float = 0.02  # $ per contract after fees
    z: float = 0.5  # require EV > z·σ(q̂) as well
    kelly_fraction: float = 0.25
    entry_style: Literal["taker", "maker", "auto"] = "auto"
    maker_min_edge: float = 0.015
    exit_premium: float = 0.02  # passive take-profit quote at q̂ + premium
    exit_slack_k: float = 0.0  # exit when liquidation ≥ q̂ − k·σ
    max_levels: int = 3  # book levels an IOC may walk
    max_disagreement: float = 0.15  # |model − mid| beyond this ⇒ distrust the model, no entry


class MarketMakerCfg(BaseModel):
    enabled: bool = False  # enable per series after paper validation
    series: list[str] = Field(
        default_factory=list
    )  # series MM quotes even if modeled (unmodeled → calibrated prior)
    gamma: float = 1.0  # risk aversion: inventory skew per unit = γ·σx²·τ (logit)
    kappa: float = 15.0  # fill-intensity decay (per logit unit)
    horizon_s: float = 3600.0
    min_edge: float = 0.01  # $ per contract after maker fee
    max_inventory: float = 50  # contracts
    quote_size: float = 10  # contracts per side
    min_seconds_to_close: int = 900
    flow_threshold: float = 0.7  # |signed flow| / volume over recent trades → pull quotes
    jump_k: float = 4.0
    min_quote_life_ms: int = 1500
    use_calibrated_prior: bool = True  # for markets without a model (only if calibration fitted)


class StrategiesCfg(BaseModel):
    structural_arb: StructuralArbCfg = StructuralArbCfg()
    edge_taker: EdgeTakerCfg = EdgeTakerCfg()
    market_maker: MarketMakerCfg = MarketMakerCfg()


class OrderGroupCfg(BaseModel):
    enabled: bool = False
    contracts_limit: int = 500  # exchange auto-cancels if exceeded within its rolling 15 s window


class BlackoutWindow(BaseModel):
    start_utc: str  # "2026-11-12T13:25:00Z"
    end_utc: str
    categories: list[str] = Field(default_factory=lambda: ["Crypto", "Financials", "Economics"])
    reason: str = ""


class RiskCfg(BaseModel):
    limits: RiskLimits = RiskLimits()
    bankroll_fraction: float = 1.0  # fraction of equity strategies may size against
    cash_buffer_usd: float = 5.0
    order_ttl_s: int = 600  # live resting orders expire unless refreshed (crash safety)
    ws_down_cancel_after_s: float = 15.0
    flatten_max_slippage: float = 0.05
    reject_storm_count: int = 10
    reject_storm_window_s: float = 60.0
    blocked_series: list[str] = Field(default_factory=list)
    blackout_windows: list[BlackoutWindow] = Field(default_factory=list)
    order_group: OrderGroupCfg = OrderGroupCfg()


class EngineCfg(BaseModel):
    autotrade: bool = False  # first run: scanner + manual only. Flip after reviewing paper results.
    tick_s: float = 0.25
    manage_manual_positions: bool = False  # let strategies exit positions they didn't open
    cancel_on_shutdown: bool = True  # live/demo: cancel strategy orders on a graceful shutdown


class ProviderCfg(BaseModel):
    kind: Literal["openai_compatible", "anthropic"] = "openai_compatible"
    base_url: str = "http://localhost:11434/v1"  # Ollama. LM Studio: http://localhost:1234/v1
    model: str = "qwen3:14b"
    api_key: SecretStr | None = None  # env: KST_LLM__PROVIDER__API_KEY (cloud only)
    timeout_s: float = 60.0
    temperature: float = 0.0
    keep_alive: str = "30m"  # Ollama: keep model + KV cache warm between calls
    extra_body: dict[str, Any] = Field(default_factory=dict)


class LLMBudgetCfg(BaseModel):
    max_calls_per_day: int = 40
    max_input_tokens_per_day: int = 60_000
    max_output_tokens_per_day: int = 20_000


class LLMCfg(BaseModel):
    mode: Literal["off", "advisory", "gate"] = "gate"
    scope: Literal["unmodeled_only", "all"] = "all"  # all = every new-market entry needs an ENTER verdict
    provider: ProviderCfg = ProviderCfg()
    escalation: ProviderCfg | None = None  # optional cloud fallback
    escalate_on_unavailable: bool = False
    batch_size: int = 15
    min_batch: int = 5
    max_wait_s: float = 20.0  # entry opportunities are perishable: don't hold a lone candidate long
    min_interval_s: float = 20.0
    cache_ttl_s: float = 2700.0
    fallback: Literal["deny", "allow"] = "deny"
    budget: LLMBudgetCfg = LLMBudgetCfg()
    bypass_strategies: list[str] = Field(default_factory=lambda: ["structural_arb"])  # never gated (riskless)


class DiscordNotifyCfg(BaseModel):
    fills: bool = True
    orders: bool = False
    risk: bool = True
    engine_state: bool = True
    daily_report: bool = True
    alerts: bool = True


class DiscordCfg(BaseModel):
    enabled: bool = False
    token: SecretStr | None = None  # env: KST_DISCORD__TOKEN
    guild_id: int | None = None  # register slash commands instantly in this guild
    channel_id: int | None = None  # notifications + control panel
    allowed_user_ids: list[int] = Field(default_factory=list)  # ONLY these users may press controls
    daily_report_time: str = "23:55"  # trading_day_tz
    min_message_interval_s: float = 1.5
    notify: DiscordNotifyCfg = DiscordNotifyCfg()


class WebCfg(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"  # non-local bind REQUIRES api_token
    port: int = 8787
    api_token: SecretStr | None = None  # env: KST_WEB__API_TOKEN
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    serve_ui: bool = True
    push_hz: float = 10.0  # max WS flush rate per client


class PaperCfg(BaseModel):
    starting_balance_usd: float = 1000.0
    maker_queue_haircut: float = 1.0  # multiply visible queue-ahead (≥1 = more conservative)


class FeesCfg(BaseModel):
    taker_coef: float = 0.07  # verify at kalshi.com/fee-schedule
    maker_ratio: float = 0.25  # maker coef = taker coef × ratio on maker-fee series
    combo_maker_ratio: float = 0.5
    rounding_allowance: float = 0.0001  # $ per fill added to EV-cost (balance-precision rounding)


class ReplayCfg(BaseModel):
    warmup_s: float = Field(default=0, ge=0)  # ingest inputs without orders before evaluating strategies
    equity_sample_s: float = Field(default=60, gt=0)  # fixed return interval for replay Sharpe estimates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KST_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppCfg = AppCfg()
    kalshi: KalshiCfg = KalshiCfg()
    universe: UniverseCfg = UniverseCfg()
    marketdata: MarketDataCfg = MarketDataCfg()
    feeds: FeedsCfg = FeedsCfg()
    pricing: PricingCfg = PricingCfg()
    scanner: ScannerCfg = ScannerCfg()
    strategies: StrategiesCfg = StrategiesCfg()
    risk: RiskCfg = RiskCfg()
    engine: EngineCfg = EngineCfg()
    llm: LLMCfg = LLMCfg()
    discord: DiscordCfg = DiscordCfg()
    web: WebCfg = WebCfg()
    paper: PaperCfg = PaperCfg()
    fees: FeesCfg = FeesCfg()
    replay: ReplayCfg = ReplayCfg()
    calibration_tools: CalibrationToolsCfg = CalibrationToolsCfg()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env > .env > YAML (passed as init kwargs) > defaults
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    @model_validator(mode="after")
    def _mode_consistency(self) -> Settings:
        if self.app.mode is Mode.DEMO and self.kalshi.env != "demo":
            self.kalshi = self.kalshi.model_copy(update={"env": "demo", "rest_url": "", "ws_url": ""})
            self.kalshi = KalshiCfg.model_validate(self.kalshi.model_dump())
        return self

    # ---- derived paths
    @property
    def data_dir(self) -> Path:
        d = self.app.data_dir
        return d if d.is_absolute() else (Path.cwd() / d)

    @property
    def db_path(self) -> Path:
        return self.data_dir / f"kst_{self.app.mode.value}.db"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(*paths: Path | str, overrides: dict[str, Any] | None = None) -> Settings:
    files = [Path(p) for p in paths] if paths else list(DEFAULT_CONFIG_FILES)
    merged: dict[str, Any] = {}
    for f in files:
        if f.is_file():
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError(f"{f}: top level must be a mapping")
            merged = _deep_merge(merged, data)
    if overrides:
        merged = _deep_merge(merged, overrides)
    return Settings(**merged)
