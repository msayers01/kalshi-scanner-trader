# Strategies and Pricing Math

This is the quantitative core. Every formula here is implemented in
`src/kst/pricing/` or `src/kst/strategies/`, and each has a unit test. If you
change a formula, change it here too.

---

## 0. Notation

| Symbol | Meaning |
|---|---|
| `p` | a YES price in dollars, `0 < p < 1` (internally `Price` = p × 10 000) |
| `a`, `b` | best YES ask, best YES bid |
| `q` | true probability that the market resolves YES |
| `q̂`, `σ_q` | our posterior estimate of `q` and its standard deviation |
| `f_t(p)`, `f_m(p)` | taker / maker fee per contract at price `p` |
| `W` | bankroll allocated to the strategy (equity × `risk.bankroll_fraction`) |
| `logit(x)` | `ln(x / (1−x))`, inverse `σ(y) = 1 / (1 + e^(−y))` |

**Fees** (`domain/fees.py`). Kalshi's general schedule is
`fee = ceil(c · C · P · (1 − P))`, where `C` is contracts and `P` is the price
in dollars.

- Taker: `c_t = 0.07 × fee_multiplier`.
- Maker on `quadratic_with_maker_fees` series: `c_m = 0.25 × c_t`.
- Maker on `quadratic_with_combo_maker_fees` series: `c_m = 0.5 × c_t`.
- Maker on plain `quadratic` series: `c_m = 0`.

`fee_type` and `fee_multiplier` come from the Series API. Fees round up to
$0.000001, then balance precision applies. For EV math we use the continuous
fee plus one balance tick (`$0.0001`) as a conservative rounding allowance.
Fees are symmetric, `f(p) = f(1 − p)`, so selling YES at `b` costs the same fee
as buying NO at `1 − b`. If the market has a `fee_waiver_expiration_time` in
the future, the fee is 0.

---

## 1. The universal decision rule

Every strategy reduces to an expected-value (EV) comparison per contract:

```
buy  YES at a :  EV_buy  = q̂ − a − f(a)
sell YES at b :  EV_sell = b − q̂ − f(b)          (same as buying NO at 1−b)
```

We trade only when:

```
EV > θ,   θ = max(min_edge, z · σ_q)
```

`min_edge` is an absolute floor (default $0.02). `z` is a robustness multiple
(default 0.5) that protects against model misspecification beyond the
posterior variance.

### 1.1 Combining a model with the market price (`pricing/base.py::combine_with_market`)

The market price is information. Ignoring it is the classic way quant models
lose money. We treat the model and the market as two noisy Gaussian
measurements of `logit(q)` and take the precision-weighted posterior:

```
ℓ_m = logit(q_model),  s_m = σ_model / (q_model (1 − q_model))  ⊕  s_floor     (model, in logit units)
ℓ_p = logit(mid),      s_p = s_p_base + ½ (logit(a) − logit(b))                 (market; wider spread ⇒ noisier)

ℓ̂  = (ℓ_m / s_m² + ℓ_p / s_p²) / (1/s_m² + 1/s_p²)
s² = 1 / (1/s_m² + 1/s_p²)
q̂  = σ(ℓ̂),   σ_q ≈ q̂ (1 − q̂) · s           (delta method)
```

`⊕` is root-sum-square. `s_floor` (default 0.15 logit) keeps any model from
being treated as perfect. `s_p_base` (default 0.35) encodes how efficient we
think Kalshi prices are. Calibrate both in paper or replay; they are the most
important hyperparameters in the system.

### 1.2 Position sizing: fractional Kelly with uncertainty shrinkage (`strategies/sizing.py`)

Buying a binary contract at all-in cost `c` (price + fee) that pays $1 with
probability `q` has the Kelly fraction of bankroll:

```
f* = (q − c) / (1 − c)
```

For NO, use `q → 1 − q̂` and `c = (1 − b) + f(b)`. Because `q̂` is uncertain,
the growth-optimal fraction shrinks (Baker & McHale, 2013, *Optimal betting
under parameter uncertainty*):

```
k     = (q̂ − c)² / ((q̂ − c)² + σ_q²)
f     = λ · k · f*                 λ = fractional-Kelly multiplier (default 0.25)
n     = floor(f · W / c)           contracts, then capped by every risk limit
```

Sizing only *increases* positions toward `n`. It never sells down because the
target shrank; exits are governed by §1.3. That avoids churn and fees.

### 1.3 Exits: EV-based, not price-based (`strategies/exits.py`)

For a long-YES position, compare selling now with holding to settlement:

```
L = b − f_t(b)          liquidation value per contract (taker exit at the bid)
H = q̂                   hold value (expected settlement payoff)

exit (taker) iff  L ≥ H − κ σ_q          κ = exit_uncertainty_slack (default 0 ⇒ pure EV)
```

While the position is held, the strategy also keeps a passive maker exit
resting at:

```
p_x = snap_up(q̂ + max(exit_premium, f_m(q̂) + tick))
```

It captures the spread if the market comes to us. NO positions are symmetric
(use the ask). There is **no naive stop-loss**. If the price falls but our
`q̂` still exceeds the liquidation value, holding has higher EV. Risk limits
(§RISK) bound the damage when the model is wrong, and the daily-loss breaker
pauses the system.

---

## 2. Strategy: `structural_arb` (coherence arbitrage)

**Markets:** any event. **Edge:** violations of probability axioms. **Risk:**
leg risk only.

### 2.1 Mutually exclusive outcomes (`event.mutually_exclusive = true`)

At most one market resolves YES.

**Sell-all basket (always valid for mutually exclusive events).** Buy NO on
every market. The cost is `Σ (1 − b_i) + Σ f(b_i)` and the payoff is at least
`n − 1`, so:

```
profit ≥ Σ b_i − 1 − Σ f(b_i)        trade iff  Σ b_i − Σ f(b_i) > 1 + margin
```

**Buy-all basket (only if outcomes are also exhaustive).** Buy YES on every
market. The cost is `Σ a_i + Σ f(a_i)` and the payoff is exactly 1:

```
trade iff  Σ a_i + Σ f(a_i) < 1 − margin
```

Exhaustiveness is verified for numeric-strike events: the lowest market is
`less`, the highest is `greater`, and the `between` buckets are contiguous with
no gaps. For other events, the event must be on `structural_arb.exhaustive_allowlist`.

### 2.2 Strike ladders (same event, same underlying, same expiry)

For `greater` strikes `K1 < K2`, monotonicity requires `P(>K1) ≥ P(>K2)`. The
pair "buy YES K1 at `a₁`, sell YES K2 at `b₂`" pays at least $1 in every state:

| State | YES K1 pays | NO K2 pays | Total |
|---|---|---|---|
| S < K1 | 0 | 1 | 1 |
| K1 ≤ S < K2 | 1 | 1 | 2 |
| S ≥ K2 | 1 | 0 | 1 |

The pair costs `a₁ + (1 − b₂)`, so:

```
trade iff  b₂ − a₁ > f(a₁) + f(b₂) + margin
```

`less` ladders use the mirror-image condition.

### 2.3 Execution

The size is the minimum available quantity across legs at the quoted prices,
capped by limits. All legs are sent at once as IOC limit orders at the
observed prices, tagged with a shared `basket_id`. After the fills, the
strategy re-evaluates residual legs: it completes them if they are still
profitable at current prices, otherwise it leaves them to the EV exit rule.

---

## 3. Fair-value model: crypto above/below and range markets (`pricing/crypto.py`)

**Underlying.** CF Benchmarks Real-Time Index (BRTI for BTC, `ETHUSD_RTI`,
etc.). Kalshi streams it on the `cfbenchmarks_value` WS channel (authenticated);
the fallback is the Coinbase Exchange public ticker, which carries basis risk.

**Settlement.** The settlement value `A` is the *simple average of the 60
one-second index values before T* (from the market's `rules_primary`).

Let `τ = T − t` (seconds), `w = 60`, and `σ_s` the per-second volatility of log
price (driftless at these horizons).

**Case `τ > w`.** The 60 s averaging removes about 2/3 of the variance inside
the window. Matching moments to a lognormal (Turnbull–Wakeman):

```
v      = σ_s² · ((τ − w) + w/3)
ln A   ~ N(ln S_t − v/2, v)
P(A>K) = 1 − F((ln(K/S_t) + v/2) / √v)
```

**Case `0 < τ ≤ w`.** We are inside the averaging window. Part of the average
is already observed: `Ā_obs` over `w − τ` seconds, from our own one-second
series. The rest has mean `S_t` and variance `S_t² σ_s² τ / 3`:

```
μ_A = ((w − τ) Ā_obs + τ S_t) / w
s_A = (τ / w) · S_t · σ_s · √(τ/3)
P(A>K) = 1 − F((K − μ_A) / s_A)
```

**Fat tails.** `F` is either the standard normal CDF, or a unit-variance
Student-t: `F(z) = T_ν(z · √(ν/(ν−2)))`, with ν = 5 by default. Short-horizon
crypto returns are leptokurtic, and the t-distribution prices the tails, which
are exactly where cheap contracts trade.

**Strike types.** `greater`/`greater_or_equal`: `P(A > floor)`. `less`/`less_or_equal`:
`P(A < cap)`. `between`: `P(floor ≤ A ≤ cap) = F(cap) − F(floor)`.

**Volatility** (`pricing/volatility.py`). EWMA of squared log returns sampled
every `Δ` seconds (default 5 s). Each squared return is divided by `Δ` to give
per-second variance. Two half-lives, fast (5 min) and slow (60 min), are
blended: `σ² = ω σ_fast² + (1 − ω) σ_slow²`. The slow estimator is seeded at
startup from 1-minute Coinbase candles (last 6 h), so the model is usable
immediately.

**Model uncertainty.** The relative standard error of the variance is
`≈ √(2 / N_eff)`, with `N_eff = (1+λ)/(1−λ)` for the EWMA. It is propagated
numerically: `σ_model = |P(σ⁺) − P(σ⁻)| / 2`. This feeds `combine_with_market`.

**Known risks.** Scheduled macro releases (CPI, FOMC) cause volatility jumps
the EWMA does not anticipate. `risk.blackout_windows` can pause new crypto
entries around them. Implied vol from options (Deribit DVOL) is on the roadmap.

---

## 4. Fair-value model: daily high temperature (`pricing/weather.py`)

**Markets.** The `KXHIGH*` series (NYC, Chicago, Miami, Austin, LA, Denver,
Philadelphia, …). They settle on the integer °F daily maximum at a specific
station for the local **climate day**, which follows standard time: during DST
it runs 1:00 AM to 12:59 AM local time.

**Strike semantics (integers).** `greater X` ⇒ `T ≥ X + 1`. `less X` ⇒
`T ≤ X − 1`. `between [lo, hi]` ⇒ `lo ≤ T ≤ hi`, inclusive.

**Forecast.** Open-Meteo ensemble API, hourly `temperature_2m` for each member
of several global ensembles (GFS 31, ECMWF IFS 51, ICON 40, GEM 21). For each
member `j`, `x_j` = the maximum of its hourly temperatures over the
*remaining* hours of the climate day, plus a station bias `b_s` (config,
default +0.5 °F, because hourly 2 m model output underestimates the true peak).

**Ensemble dressing** (Roulston & Smith 2003; Wang & Bishop 2005). Each
member becomes a Gaussian kernel. The bandwidth inflates the ensemble spread
to correct the known under-dispersion:

```
h = max(h_min, α · std(x) · J^(−1/5))         (Silverman-style, α = inflation, default 1.3; h_min = 1.0 °F)
P(X ≤ k) = (1/J) Σ_j Φ((k + 0.5 − x_j) / h)
```

**Intraday conditioning.** If the observed maximum so far today is `M` (NWS
station observations), the final maximum is `T = max(M, X_remaining)`:

```
P(T ≤ k) = 0               for k < M
P(T ≤ k) = P(X ≤ k)        for k ≥ M         (mass P(X ≤ M) collapses onto M)
```

This is the correct update when `X` describes only the remaining hours. As the
day ends, `X` collapses toward the current temperature and the distribution
concentrates on `M`.

**Uncertainty.** `σ_model = √(q(1−q) / J)` (ensemble sampling) ⊕ a
model-error floor.

**Known risks.** Settlement-source revisions, station outages, and the rules
now citing "The Weather Company" (verify the station per market). Bias `b_s`
should be fitted per station from history (roadmap: `kst calibrate-weather`).

---

## 5. Strategy: `edge_taker` (model edge, taker or patient maker entry)

**Markets:** anything the model resolver covers (crypto, weather).

1. `q̂, σ_q` come from the model, combined with the market (§1.1).
2. If `EV_buy` or `EV_sell` at the touch exceeds `θ`, take liquidity with an
   IOC limit at the touch, walking the book only while the marginal EV stays
   above `θ`, up to the Kelly size (§1.2).
3. Otherwise, if `entry_style ∈ {maker, auto}` and a post-only quote one tick
   inside the spread would have `EV > θ_maker` (with the maker fee), rest that
   quote. It is cancelled when the edge disappears.
4. Positions are managed by the exit rule (§1.3).

---

## 6. Strategy: `market_maker` (Avellaneda–Stoikov in logit space)

Binary prices live in (0, 1), so we model the **log-odds** `x = logit(fair)` as
Brownian with volatility `σ_x` (EWMA of logit-mid changes). With inventory `I`
(signed contracts / `unit_size`), risk aversion `γ`, horizon
`τ = min(T_close − t, H)`, and fill-intensity decay `κ`:

```
reservation   r = x̂ − I · γ · σ_x² · τ
total spread  Δ = γ σ_x² τ + (2/γ) · ln(1 + γ/κ)
bid = snap_down(σ(r − Δ/2)),   ask = snap_up(σ(r + Δ/2))
```

**Constraints, in order.**

- Post-only: `bid < best_ask` and `ask > best_bid`.
- Maker EV floor: `q̂ − bid − f_m(bid) ≥ mm_min_edge` and
  `ask − q̂ − f_m(ask) ≥ mm_min_edge`.
- Inventory caps: no bid when `I ≥ I_max`, no ask when `I ≤ −I_max`.
- Size tapers linearly with `|I|`.

**Toxicity filters (pull quotes).**

- Signed trade-flow imbalance over the last N trades exceeds `flow_threshold`.
- A jump: `|Δx| > k σ_x √Δt` since the last tick.
- The reference feed is stale.
- Time-to-close is below `mm.min_time_to_close_s`. Adverse selection peaks
  near resolution.

**Fair value `x̂`.** The model `q̂` when available, otherwise the microprice
`(b·Q_a + a·Q_b)/(Q_a + Q_b)`, optionally passed through the calibrated prior
(§7).

**Quote maintenance.** The OMS amends only when the price moves by at least 1
tick or the size changes by at least 20%. A `min_quote_life_ms` limit
respects rate limits.

---

## 7. Calibrated prior: the favorite-longshot bias (`pricing/calibration.py`)

Bürgi, Deng & Whelan (2025), *Makers and Takers: The Economics of the Kalshi
Prediction Market* (UCD WP25/19, CEPR DP20631), studied 300 000+ contracts:

- Kalshi prices are informative.
- **Low-priced contracts win less often than their price implies.**
- Favorites earn small positive returns.
- Makers out-earn takers.

We model this as a one-parameter-pair recalibration:

```
logit(q) = α + β · logit(p)          (β > 1 ⇒ extremes are more extreme than prices say)
```

The fit is a logistic regression of settlement outcomes on `logit(price at
horizon h before close)`, from historical settled markets
(`pricing.calibration.fit_logistic_calibration`). The fit covariance gives
`σ_model` by the delta method. Fits are stored in `data/calibration.json`, per
category if there is enough data.

**The defaults are the identity (α = 0, β = 1), which gives no edge.** The
prior generates trades only after you run a fit and review it. Used with
`market_maker` in maker-only mode, it systematically sells overpriced
longshots and supplies liquidity to takers. That is the empirically profitable
side of Kalshi's flow.

---

## 8. Routing (`strategies/router.py`)

The router applies rules in priority order. The first match wins, per market:

1. Markets in an event with a live arb signal → `structural_arb`. It works at
   the event level and runs alongside the other strategies.
2. Markets with a fair-value model (`pricing/resolver.py`) → `edge_taker`,
   plus `market_maker` if that is enabled for the series.
3. Other markets, only if the LLM gate approves → `market_maker` in calibrated
   maker-only mode, when the calibration is fitted. Otherwise they are
   **not traded**.

The router never trades a market its strategy cannot price. "No model, no
trade" is the default.
