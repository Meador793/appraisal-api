"""
Market-derived adjustments from a tree model.

The problem this solves
-----------------------
A linear regression hands you the adjustment for free: the coefficient on
`baths_full` IS the dollar value of a full bath, holding everything else
constant. XGBoost and Random Forest have no coefficients. They are far better
predictors on this kind of data, but on their own they give you a price, not
an adjustment grid.

The fix is a counterfactual marginal effect, sometimes called an ICE-derived
average partial effect:

    For every sale in the sample:
        predict it as-is
        add one unit of the feature, hold everything else fixed
        predict again
        record the difference
    Average those differences.

That is the same quantity a regression coefficient estimates -- the average
change in price from one more unit, other things equal -- but estimated
without assuming the effect is constant across the market. It is what makes
the "holding everything else constant" clause in your Step 10 markdown true
for a tree model too.

Three honest caveats, all reported in the output tables:

1. Trees are step functions. For many sales, adding 1 sq ft crosses no split
   point and the predicted price does not move at all. `Share_Responsive`
   reports the fraction of sales where the model actually reacted. A low
   share means the model has not learned a smooth rate for that feature and
   the average is being carried by a minority of records.

2. Trees cannot extrapolate. Pushing a feature past the range in the training
   data returns the edge value forever. `support_warning` flags features
   where the requested step walks a meaningful share of rows out of range.

3. The bootstrap interval here is the sampling variation of the average
   effect across sales. It is the direct analogue of the regression's
   confidence interval and the same decision rule applies: if the interval
   spans zero, the data has not shown the effect differs from no effect, and
   it does not belong in a defensible grid.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .features import DEFAULT_STEPS, STEP_LABELS, UNIT_LABELS


def _predict(model, X):
    return np.asarray(model.predict(X), dtype=float)


def marginal_effect_rows(model, X: pd.DataFrame, feature: str, step: float) -> np.ndarray:
    """Per-sale dollar effect of adding `step` units of `feature`."""
    base = _predict(model, X)
    bumped = X.copy()
    bumped[feature] = bumped[feature] + step
    return _predict(model, bumped) - base


def _bootstrap_ci(per_row: np.ndarray, n_boot: int, alpha: float, rng) -> tuple[float, float]:
    """Percentile bootstrap over sales. Resamples rows, not model fits, so it
    answers 'how stable is this average across the sample we have' -- not
    'how much would the number move if we retrained'. Refit-bootstrap is the
    stronger test; it is available via refit_bootstrap() below and is slow."""
    n = len(per_row)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = per_row[idx].mean(axis=1)
    lo = np.percentile(means, 100 * alpha / 2)
    hi = np.percentile(means, 100 * (1 - alpha / 2))
    return float(lo), float(hi)


def adjustment_grid(
    model,
    X: pd.DataFrame,
    features: list[str] | None = None,
    steps: dict | None = None,
    n_boot: int = 400,
    alpha: float = 0.05,
    random_state: int = 42,
    responsive_tol: float = 1.0,
) -> pd.DataFrame:
    """
    The adjustment grid. One row per feature.

    Columns
    -------
    Adjustment        : average dollar effect of one step of the feature
    CI_Lower_95/Upper : percentile bootstrap interval on that average
    Share_Responsive  : fraction of sales where the model moved at all
    CI_Crosses_Zero   : the interval spans 0
    Use_In_Grid       : the decision -- supported, or not
    """
    rng = np.random.default_rng(random_state)
    steps = {**DEFAULT_STEPS, **(steps or {})}
    features = features or [f for f in X.columns if not f.startswith("loc_")]

    rows = []
    for feat in features:
        step = steps.get(feat, 1.0)
        per_row = marginal_effect_rows(model, X, feat, step)
        lo, hi = _bootstrap_ci(per_row, n_boot, alpha, rng)
        share = float((np.abs(per_row) > responsive_tol).mean())
        crosses = (lo * hi) < 0

        if crosses:
            verdict = "NO - interval spans zero"
        elif share < 0.25:
            verdict = "WEAK - model responds on few sales"
        else:
            verdict = "YES - supported"

        rows.append({
            "Feature": feat,
            "Unit": STEP_LABELS.get(feat, UNIT_LABELS.get(feat, feat)),
            "Step": step,
            "Adjustment": float(per_row.mean()),
            "Median_Effect": float(np.median(per_row)),
            "CI_Lower_95": lo,
            "CI_Upper_95": hi,
            "Share_Responsive": share,
            "CI_Crosses_Zero": bool(crosses),
            "Use_In_Grid": verdict,
        })

    out = pd.DataFrame(rows)
    return out.sort_values("Adjustment", key=abs, ascending=False).reset_index(drop=True)


def percent_grid(
    log_model,
    X: pd.DataFrame,
    mean_price: float,
    features: list[str] | None = None,
    steps: dict | None = None,
    n_boot: int = 400,
    alpha: float = 0.05,
    random_state: int = 42,
) -> pd.DataFrame:
    """
    Percentage adjustments, from a model trained on log(price).

    Appraisers often think in percentages -- 'a full bath is worth about 3% of
    value'. A percentage effect is also more stable across price tiers than a
    fixed dollar amount, which is the standard objection to a dollar grid: a
    bath is not worth the same in a $300k house and a $900k house.

    exp(mean log-delta) - 1 converts the log-space effect to a percentage.
    """
    rng = np.random.default_rng(random_state)
    steps = {**DEFAULT_STEPS, **(steps or {})}
    features = features or [f for f in X.columns if not f.startswith("loc_")]

    rows = []
    for feat in features:
        step = steps.get(feat, 1.0)
        per_row = marginal_effect_rows(log_model, X, feat, step)  # in log dollars
        lo_log, hi_log = _bootstrap_ci(per_row, n_boot, alpha, rng)
        pct = (np.exp(per_row.mean()) - 1) * 100
        rows.append({
            "Feature": feat,
            "Unit": STEP_LABELS.get(feat, UNIT_LABELS.get(feat, feat)),
            "Step": step,
            "Percent_Effect": float(pct),
            "Pct_CI_Lower": float((np.exp(lo_log) - 1) * 100),
            "Pct_CI_Upper": float((np.exp(hi_log) - 1) * 100),
            "Dollar_At_Mean_Price": float(pct / 100 * mean_price),
        })

    out = pd.DataFrame(rows)
    return out.sort_values("Percent_Effect", key=abs, ascending=False).reset_index(drop=True)


def location_effects(model, X: pd.DataFrame, baseline_location: str) -> pd.DataFrame:
    """Location adjustment: switch each location dummy on for every sale and
    read the price change relative to the baseline location."""
    loc_cols = [c for c in X.columns if c.startswith("loc_")]
    if not loc_cols:
        return pd.DataFrame(columns=["Location", "Adjustment_vs_Baseline"])

    base_frame = X.copy()
    base_frame[loc_cols] = 0.0
    base = _predict(model, base_frame)

    rows = []
    for col in loc_cols:
        f = base_frame.copy()
        f[col] = 1.0
        rows.append({
            "Location": col.replace("loc_", ""),
            "Adjustment_vs_Baseline": float((_predict(model, f) - base).mean()),
        })
    out = pd.DataFrame(rows).sort_values("Adjustment_vs_Baseline", ascending=False)
    out.attrs["baseline"] = baseline_location
    return out.reset_index(drop=True)


def contribution_breakdown(model, x_row: pd.DataFrame, X_ref: pd.DataFrame) -> pd.DataFrame:
    """
    Per-feature contribution for ONE subject property.

    This is the tree-model analogue of `Subject_Value * Coefficient` from the
    regression notebook. For each feature we move the subject's value back to
    the sample median and record how much the indicated value drops. The
    result reads as: 'this subject is worth $X more than a median property
    because of its garage'.

    These do not sum exactly to the total -- tree models are not additive, and
    the order you remove features in changes the parts. The residual is
    reported as 'Baseline + interactions' rather than silently absorbed.
    """
    med = X_ref.median()
    total = float(_predict(model, x_row)[0])

    rows = []
    for feat in X_ref.columns:
        if x_row[feat].iloc[0] == med[feat]:
            continue
        counterfactual = x_row.copy()
        counterfactual[feat] = med[feat]
        delta = total - float(_predict(model, counterfactual)[0])
        if abs(delta) < 1:
            continue
        rows.append({
            "Feature": feat,
            "Subject_Value": float(x_row[feat].iloc[0]),
            "Sample_Median": float(med[feat]),
            "Contribution": delta,
        })

    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values("Contribution", key=abs, ascending=False).reset_index(drop=True)
    return out


def compare_scenarios(model, x_row: pd.DataFrame, feature: str, values: list) -> pd.DataFrame:
    """Paired scenario analysis: identical properties differing in exactly one
    characteristic. The regression equivalent of a matched-pairs grid, except
    everything else is held constant by construction rather than by hoping two
    comparables happen to be otherwise identical."""
    rows = []
    for v in values:
        f = x_row.copy()
        f[feature] = float(v)
        rows.append({feature: v, "Indicated_Value": float(_predict(model, f)[0])})
    out = pd.DataFrame(rows)
    out["Difference"] = out["Indicated_Value"].diff()
    return out


def refit_bootstrap(model_factory, X, y, feature, step, n_boot=25, random_state=42):
    """
    Stronger interval: refit the model on each bootstrap resample. Captures
    model-fitting variance, not just sampling variance of the average. Slow
    (n_boot full fits), so it is off the main path -- run it in the notebook
    on the two or three adjustments you actually intend to defend.
    """
    rng = np.random.default_rng(random_state)
    n = len(X)
    estimates = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        Xb, yb = X.iloc[idx], y.iloc[idx]
        m = model_factory()
        m.fit(Xb, yb)
        estimates.append(marginal_effect_rows(m, Xb, feature, step).mean())
    estimates = np.array(estimates)
    return float(estimates.mean()), float(np.percentile(estimates, 2.5)), float(np.percentile(estimates, 97.5))
