# Calibration tools

`kst calibrate` fits the favorite–longshot probability prior, globally and per category.
`kst calibrate-weather` fits daily-high bias and ensemble spread per station. Both generate
**candidate artifacts** and chronological validation reports. They do not replace active files,
change autotrading, or restart the app.

## Market probabilities

```powershell
# Offline, repeatable samples. --input can be repeated; CSV or JSONL.
uv run kst calibrate --input data/market-samples.jsonl --output data/calibrations/market-candidate

# Bounded public history, covering both recent and historical storage.
uv run kst calibrate --series KXBTCD --max-markets 1000 --output data/calibrations/btc-candidate
```

When the UI's `kst.exe` is already running on Windows, use `uv run --no-sync kst ...`
to run these commands without trying to reinstall that locked executable. The new plotting
dependency is in `pyproject.toml`/`uv.lock`; a normal `uv sync --all-extras` installs it when the
UI is stopped. Plotting uses Matplotlib and creates PNG/SVG artifacts without a display.

Each normalized sample contains exactly one market at one configured horizon:

```json
{"ticker":"S-E-M","event_ticker":"S-E","category":"Crypto","price":0.42,"outcome":1,"observed_ms":1704067200000,"close_ms":1704070800000,"settled_ms":1704074400000,"synthetic":false}
```

Prices are probabilities in YES space, inside `(0,1)`. Outcomes are integer `0` or `1`.
Times are Unix **milliseconds**. The observation must be at or before close minus
`calibration_tools.horizon_h`, no older than `max_quote_age_s` before that target. Labels must be
available at `settled_ms`, not after `--as-of-ms` (default now). CSV uses the same column names.
Exact duplicate rows are deduplicated; conflicting rows for the same market are errors.

Acquisition reads final binary outcomes and both live/recent and archived market partitions.
It queries only 1-minute candles ending no later than the target. It selects the most recent
valid YES bid/ask midpoint within the quote-age window; crossed/empty/wide quotes, zero-volume
bars, missing traded closes, provisional outcomes and scalar payouts are excluded. Archive
404s trigger a boundary fallback to the other candle endpoint. Current series metadata supplies
category labels; pre-horizon candles supply all price features. The cohort is bounded, not a
claim of complete historical coverage. Per-series/per-tier listing visits and the final candle
cohort are capped by `max_markets`; evidence and exclusion counts are saved.

The newest fraction of **events** is held out. All related strikes stay together. Training
events whose labels were not known before the first held-out prediction are purged. Defaults
require 200 training samples/20 events and 50 holdout samples/10 events, with both outcome classes
in each. Categories lacking the same minimum coverage fall back to the global fit.

Coefficients minimize penalized binary log loss:

`q = sigmoid(alpha + beta * logit(p))`

The ridge penalty shrinks toward `(alpha,beta)=(0,1)`. The stable optimizer uses `logaddexp`
and `expit`; covariance is evaluated at the fitted parameters and replaced with an event-cluster
sandwich estimate for the exported job. Negative/nonpositive slopes are rejected for review.
Candidate coefficients use **training data only**, not a refit on holdout outcomes.

Outputs: `calibration.json`, `report.json`, `samples.jsonl`, `reliability.png`, `reliability.svg`,
and (for API downloads) `evidence.jsonl`. Reports include Brier score, log loss, expected calibration
error, per-bin reliability counts, category coverage, label/prediction timing, input hashes and a
`heldout_improves` indicator. Reliability plots use held-out events only. Small-bin frequency
changes should not be interpreted as statistical significance or after-fee trading edge.

To use a reviewed candidate, point `pricing.calibration.path` in local configuration at its
`calibration.json`, then restart the app. The prior respects the recorded horizon/tolerance:
a one-hour fit is not applied several hours before close. Legacy files with `horizon_h: 0`
remain unrestricted. Fits cannot be used before their `fitted_at` timestamp.

## Weather calibration

```powershell
# Already paired pre-day daily-max ensembles and final reported highs.
uv run kst calibrate-weather --input data/weather-samples.jsonl --output data/calibrations/weather-candidate

# Pair recorded hourly ensembles with source-labeled final station/day reports.
uv run kst calibrate-weather --recordings "data/recordings/*.jsonl.gz" --observations data/final-highs.csv --output data/calibrations/weather-archive
```

Paired JSONL format (the example is abbreviated; defaults require at least ten complete members):

```json
{"series":"KXHIGHNY","station_id":"KNYC","day":"2024-01-01","issued_ms":1704063600000,"members_max_f":[40.1,42.3,43.2],"observed_high_f":44,"observed_available_ms":1704200400000,"source":"Final NWS CLI report identifier","synthetic":false}
```

`issued_ms` means forecast **availability**, including receipt delay. It must be before the
station's climate day and inside `weather_max_lead_h` (default 24 h). `observed_high_f` is the
final integer-Fahrenheit daily high, with `observed_available_ms` after the climate day and
before the calibration cutoff. The source string identifies the final climatological report
or settlement source. Intraday observed maxima are not final daily labels. Verify the station
and source against market rules; the tool checks the configured station ID, not the authenticity
of a supplied report. Direct paired inputs must use pre-day operational forecasts, not reanalysis.

For archive pairing, observation CSV/JSONL omits `issued_ms` and `members_max_f` and retains the
other fields. The tool selects the latest forecast received **before** the climate day. Hourly
members must cover all 24 standard-time climate-day hours; members with any missing hour are
dropped. Forecast hours outside that day are excluded. Missing pairs are counted by station.
Daily climate windows follow the production model's standard-time convention, including DST.

Newest dates across **all stations** are reserved for holdout. Training days with final reports
not available before the first validation forecast are purged. Defaults require at least 60
training and 20 holdout days per station. Sparse stations retain existing settings in the overlay.

The fit minimizes the negative log probability of the reported integer daily high under the
production Gaussian kernel mixture. It uses exactly the same bandwidth formula and floor:

`h = max(h_min, inflation * sd * J^(-1/5), inflation * sd / 2)`

The outcome mass is `CDF(high) - CDF(high-1)`, including Fahrenheit half-degree rounding
boundaries. Stable log-CDF differences avoid underflow in the tails. Bias and inflation bounds
are configurable; boundary fits are flagged in the report.

Outputs: `weather.yaml`, `report.json`, `samples.jsonl`, `weather-validation.png` and `.svg`.
Reports compare held-out daily log loss, MAE and RMSE against current settings. The YAML contains
complete station definitions with fitted `bias_f` and `spread_inflation`, so it can be loaded as
an additional configuration overlay. It also records calibration time, synthetic status and the
bandwidth floor. Review the report and keep the same floor/configuration when using the fit.
Do not apply a weather fit to replay dates preceding its training cutoff; replay's identity-prior
override does not remove explicitly supplied weather configuration.

## Synthetic functionality check

```powershell
uv run --no-sync python examples/generate-calibration-demo.py
uv run --no-sync kst calibrate --input data/calibration-demo-inputs/markets.jsonl --output data/calibrations/demo-market
uv run --no-sync kst calibrate-weather --input data/calibration-demo-inputs/weather.jsonl --output data/calibrations/demo-weather
```

Every generated sample, report and plot is labeled synthetic. It is not historical performance.
Existing output directories are rejected. New directories and immutable input snapshots preserve
review history. The tools refuse mixed synthetic/historical datasets and insufficient coverage.

All operating defaults are under `calibration_tools` in `config/default.yaml`. Use a configuration
overlay for alternate horizons, minimum sample sizes, holdout fraction, weather lead/bounds and
regularization. Repeatedly choosing settings from the same holdout consumes that holdout; reserve
another later period or a forward paper run for independent evaluation.

API references: [Historical storage](https://docs.kalshi.com/getting_started/historical_data),
[archived markets](https://docs.kalshi.com/api-reference/historical/get-historical-markets),
[historical candles](https://docs.kalshi.com/api-reference/historical/get-historical-market-candlesticks).
