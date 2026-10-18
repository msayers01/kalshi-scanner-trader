"""Weather inputs: Open-Meteo ensemble forecasts (hourly 2 m temperature per member) and NWS station
observations (intraday max so far). Both are free and keyless; NWS asks for a contact User-Agent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from kst.config import WeatherFeedCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock, ms_to_dt
from kst.core.events import FeedHealth
from kst.core.logging import get_logger
from kst.marketdata.catalog import MarketCatalog
from kst.pricing.weather import EnsembleForecast, Observation, WeatherStore, climate_day_window_ms

log = get_logger(__name__)

OPEN_METEO = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS = "https://api.weather.gov"


def parse_open_meteo(data: dict[str, Any], issued_ms: int) -> EnsembleForecast | None:
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    cols = [k for k in hourly if k.startswith("temperature_2m")]
    if not times or not cols:
        return None
    members = np.array([[np.nan if v is None else float(v) for v in hourly[c]] for c in cols], dtype=float)
    return EnsembleForecast(
        issued_ms=issued_ms, times_ms=np.array(times, dtype=np.int64) * 1000, members_f=members
    )


class WeatherFeed:
    def __init__(
        self,
        cfg: WeatherFeedCfg,
        store: WeatherStore,
        catalog: MarketCatalog,
        bus: EventBus,
        clock: Clock,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.catalog = catalog
        self.bus = bus
        self.clock = clock
        self.http = http or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": cfg.user_agent})

    def _active_series(self) -> list[str]:
        """Only fetch stations that currently have markets in the universe (saves requests)."""
        live = {m.series_ticker for m in self.catalog.universe_markets()}
        return [s for s in self.cfg.stations if s in live] or []

    async def fetch_forecast(self, series: str) -> None:
        st = self.cfg.stations[series]
        params: dict[str, Any] = {
            "latitude": st.lat,
            "longitude": st.lon,
            "hourly": "temperature_2m",
            "models": ",".join(self.cfg.models),
            "temperature_unit": "fahrenheit",
            "forecast_days": 3,
            "past_days": 1,
            "timezone": "GMT",
            "timeformat": "unixtime",
        }
        r = await self.http.get(OPEN_METEO, params=params)
        r.raise_for_status()
        fc = parse_open_meteo(r.json(), self.clock.now_ms())
        if fc is not None:
            self.store.forecasts[series] = fc
            log.info(
                "weather_forecast", series=series, members=fc.members_f.shape[0], hours=fc.members_f.shape[1]
            )

    async def fetch_observations(self, series: str) -> None:
        st = self.cfg.stations[series]
        now = self.clock.now_ms()
        # Today's climate day for the station.
        today = ms_to_dt(now).astimezone(ZoneInfo(st.tz)).date()
        start, _ = climate_day_window_ms(today, st.tz)
        if now < start:
            return
        iso = datetime.fromtimestamp(start / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = await self.http.get(
            f"{NWS}/stations/{st.nws_station}/observations", params={"start": iso, "limit": 500}
        )
        r.raise_for_status()
        temps_c = [
            f["properties"]["temperature"]["value"]
            for f in (r.json().get("features") or [])
            if f.get("properties", {}).get("temperature", {}).get("value") is not None
        ]
        if temps_c:
            max_f = max(temps_c) * 9.0 / 5.0 + 32.0
            self.store.observations[series] = Observation(today, round(max_f, 1), now)

    async def run(self) -> None:
        last_fc: dict[str, int] = {}
        last_obs: dict[str, int] = {}
        while True:
            now = self.clock.now_ms()
            ok = True
            for series in self._active_series():
                try:
                    if now - last_fc.get(series, 0) >= self.cfg.refresh_s * 1000:
                        await self.fetch_forecast(series)
                        last_fc[series] = now
                    if now - last_obs.get(series, 0) >= self.cfg.obs_refresh_s * 1000:
                        await self.fetch_observations(series)
                        last_obs[series] = now
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    ok = False
                    log.warning("weather_fetch_failed", series=series, error=str(exc))
            self.bus.publish(FeedHealth(self.clock.now_ms(), "weather", ok))
            await asyncio.sleep(30.0)
