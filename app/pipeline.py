"""
Training pipeline as a function.

The notebook is for looking at one dataset carefully -- plots, diagnostics,
judgement. This module is the same pipeline with the looking removed, so you
can run it over several MLS exports and compare them side by side.

Both import `features.py` and `adjustments.py`, so there is one definition of
every variable and one definition of every adjustment. The notebook and this
file cannot disagree about what `gla_sqft` means.

    from app.pipeline import run_dataset
    result = run_dataset(CONFIG_CARMEL)
    print(result["comparison"])

Deliberately NOT here: any notion of combining datasets. Each call trains one
model on one market and writes its own versioned artifacts. Pooling two markets
into one model would force a single adjustment grid across both, which is the
opposite of what you want when the question is "does this market price a garage
differently".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from scipy.stats import randint
from sklearn.ensemble import RandomForestRegressor
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import KFold, RandomizedSearchCV, cross_val_score

from .adjustments import adjustment_grid, location_effects, percent_grid
from .features import build_design_matrix, build_training_frame


def _tune_xgboost(X_train, y_train, dates_train, mono, cfg):
    """
    Learning-rate search with early stopping, in place of one fixed
    learning_rate=0.05. For each candidate rate, train with a generous
    n_estimators ceiling and let early stopping find how many trees that
    rate actually needs; keep whichever (rate, tree count) pair gets the
    lowest validation error.

    The validation split is carved from the TRAINING period only, by date
    (the last slice of X_train), never from X_test. X_test is the true
    held-out later period used for the reported model-comparison metrics,
    and it must stay untouched by anything that influenced how the model
    was tuned -- otherwise the reported accuracy is optimistic.

    Below a minimum sample size the search is skipped outright rather than
    run on a validation slice too small to mean anything, mirroring the
    same honesty rule already used for cross-validation and RF tuning below.
    """
    min_rows = cfg.get("xgb_tune_min_rows", 40)
    if len(X_train) < min_rows:
        return None, None, None

    rates = cfg.get("xgb_learning_rates", [0.03, 0.05, 0.08, 0.12])
    early_stop = cfg.get("xgb_early_stopping_rounds", 30)
    random_state = cfg.get("random_state", 42)

    val_cutoff = dates_train.quantile(0.80)
    fit_mask = dates_train <= val_cutoff
    Xf, Xv = X_train[fit_mask], X_train[~fit_mask]
    yf, yv = y_train[fit_mask], y_train[~fit_mask]
    if len(Xv) < 5 or len(Xf) < min_rows - 10:
        return None, None, None

    best = None  # (mae, rate, n_estimators)
    for rate in rates:
        m = xgb.XGBRegressor(
            n_estimators=1200, learning_rate=rate, max_depth=4, subsample=0.8,
            colsample_bytree=0.8, min_child_weight=5, reg_lambda=1.0,
            random_state=random_state, monotone_constraints=mono,
            early_stopping_rounds=early_stop, n_jobs=-1,
        )
        m.fit(Xf, yf, eval_set=[(Xv, yv)], verbose=False)
        pred = m.predict(Xv)
        mae = mean_absolute_error(yv, pred)
        trees = (m.best_iteration or 0) + 1
        if best is None or mae < best[0]:
            best = (mae, rate, trees)

    return best[1], best[2], best[0]  # rate, n_estimators, validation_mae


def _tune_random_forest(X_train, y_train, cfg):
    """
    RandomizedSearchCV in place of one fixed (n_estimators=500,
    min_samples_leaf=2). Samples a bounded number of hyperparameter
    combinations rather than an exhaustive grid -- a full grid over five
    parameters is hundreds of fits before cross-validation even multiplies
    it further, which does not fit a Lambda invocation's time or cost
    budget for the accuracy it adds over a well-chosen random sample.

    CV fold count is capped by sample size for the same reason as the
    cross-validation step elsewhere in this file: a fold with too few rows
    produces a meaningless score, not a conservative one. Below a minimum
    sample size the search is skipped and the previous fixed defaults are
    used instead. Above roughly 1,000 rows, the search budget (n_iter)
    scales DOWN rather than staying fixed -- each individual forest fit
    gets slower as rows grow, so a fixed n_iter that is safely fast on a
    few hundred rows can approach Lambda's timeout on a genuinely large
    metro-wide export.
    """
    min_rows = cfg.get("rf_tune_min_rows", 30)
    n_rows = len(X_train)
    if n_rows < min_rows:
        return None

    if n_rows < 1000:
        n_iter = cfg.get("rf_search_n_iter", 20)
    elif n_rows < 3000:
        n_iter = cfg.get("rf_search_n_iter_medium", 12)
    else:
        n_iter = cfg.get("rf_search_n_iter_large", 6)
    cv_folds = max(2, min(5, n_rows // 15))
    random_state = cfg.get("random_state", 42)

    param_dist = {
        "n_estimators": randint(200, 800),
        "max_depth": [5, 8, 12, 16, None],
        "min_samples_split": randint(2, 20),
        "min_samples_leaf": randint(1, 10),
        "max_features": ["sqrt", 0.5, 0.8, None],
    }
    search = RandomizedSearchCV(
        estimator=RandomForestRegressor(random_state=random_state, n_jobs=-1),
        param_distributions=param_dist, n_iter=n_iter,
        scoring="neg_mean_absolute_error", cv=cv_folds,
        random_state=random_state, n_jobs=-1,
    )
    search.fit(X_train, y_train)
    return search.best_params_


def load_dataset(cfg: dict) -> pd.DataFrame:
    path = Path(cfg["excel_path"])
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Fix CONFIG['excel_path'] for dataset "
            f"'{cfg.get('dataset_name', cfg['model_version'])}'."
        )
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path)
    return pd.read_excel(path, sheet_name=cfg.get("sheet_name", 0))


def diagnostics(data: pd.DataFrame) -> dict:
    """Diagnostic A from the notebook, as data rather than printed text."""
    price = data["sale_price"]
    d = {
        "price_range_ratio": float(price.max() / price.min()),
        "price_cv": float(price.std() / price.mean()),
        "raw_median_ppsf": float(data["ppsf"].median()),
        "gla_corr": float(price.corr(data["gla_sqft"])),
    }
    flags = []
    if d["price_range_ratio"] < 2.0:
        flags.append(f"Price span only {d['price_range_ratio']:.2f}x -- filtered pull.")
    if d["price_cv"] < 0.20:
        flags.append(f"Coefficient of variation {d['price_cv']:.3f} -- filtered pull.")
    if d["gla_corr"] < 0.60:
        flags.append(f"Price-to-GLA correlation only {d['gla_corr']:.3f}.")
    d["flags"] = flags
    return d


def run_dataset(cfg: dict, df_raw: pd.DataFrame | None = None,
                write_artifacts: bool = True, verbose: bool = True) -> dict:
    """
    Train and evaluate one dataset. Returns everything needed to compare it
    against another, and optionally writes the five Phase 1 artifacts.
    """
    name = cfg.get("dataset_name", cfg["model_version"])
    say = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    say(f"\n{'=' * 72}\n{name}\n{'=' * 72}")

    # ---------------------------------------------------------------- load
    if df_raw is None:
        df_raw = load_dataset(cfg)
    df_raw = df_raw.drop_duplicates()

    df_sold = df_raw[df_raw[cfg["status_col"]] == cfg["status_keep"]].copy()
    df_sold = df_sold[df_sold[cfg["target"]].notna()]
    say(f"Closed sales: {len(df_sold)} of {len(df_raw)} records")

    # ------------------------------------------------------------ features
    data, meta_bits = build_training_frame(df_sold, cfg)
    X, y, baseline_loc, location_levels = build_design_matrix(
        data, cfg.get("min_location_count", 15))
    dropped = X.attrs.get("dropped_features", [])

    if meta_bits["missing_fields"]:
        say(f"  Fields absent from this export: {', '.join(meta_bits['missing_fields'])}")
    if dropped:
        say(f"  Features dropped for no variation: {', '.join(dropped)}")

    if len(X) < 60:
        say(f"  WARNING: only {len(X)} usable sales. Adjustments from a sample this "
            "small are indicative at best, whatever their confidence intervals say.")

    diag = diagnostics(data)
    for f in diag["flags"]:
        say(f"  RANGE RESTRICTION: {f}")

    # --------------------------------------------------------------- split
    dates = data.loc[X.index, "close_date"]
    cutoff = (pd.Timestamp(cfg["train_through"]) if cfg.get("train_through")
              else dates.quantile(1 - cfg.get("test_fraction", 0.20)))
    train_mask = dates <= cutoff
    X_train, X_test = X[train_mask], X[~train_mask]
    y_train, y_test = y[train_mask], y[~train_mask]
    say(f"  Time split at {pd.Timestamp(cutoff).date()}: "
        f"{len(X_train)} train / {len(X_test)} test")

    # ------------------------------------------------------------ XGBoost
    mono = tuple(cfg.get("monotone", {}).get(c, 0) for c in X.columns)
    dates_train = dates[train_mask]

    tuned_rate, tuned_trees, tune_val_mae = _tune_xgboost(X_train, y_train, dates_train, mono, cfg)
    if tuned_rate is not None:
        say(f"  XGBoost tuning: learning_rate={tuned_rate}, {tuned_trees} trees "
            f"(validation MAE ${tune_val_mae:,.0f})")
        learning_rate, n_estimators = tuned_rate, tuned_trees
    else:
        say("  Skipping XGBoost learning-rate search (sample too small); using defaults.")
        learning_rate, n_estimators = 0.05, 800

    params = dict(
        n_estimators=n_estimators, learning_rate=learning_rate, max_depth=4, subsample=0.8,
        colsample_bytree=0.8, min_child_weight=5, reg_lambda=1.0,
        random_state=cfg.get("random_state", 42),
        monotone_constraints=mono, n_jobs=-1,
    )
    xgb_model = xgb.XGBRegressor(**params).fit(X_train, y_train)
    xp = xgb_model.predict(X_test)
    r2_x = r2_score(y_test, xp)
    mae_x = mean_absolute_error(y_test, xp)
    rmse_x = float(np.sqrt(mean_squared_error(y_test, xp)))

    # ------------------------------------------------------ Random Forest
    tuned_rf_params = _tune_random_forest(X_train, y_train, cfg)
    if tuned_rf_params is not None:
        say(f"  Random Forest tuning: {tuned_rf_params}")
        rf_params = {**tuned_rf_params, "random_state": cfg.get("random_state", 42), "n_jobs": -1}
    else:
        say("  Skipping Random Forest hyperparameter search (sample too small); using defaults.")
        rf_params = dict(n_estimators=500, min_samples_leaf=2,
                         random_state=cfg.get("random_state", 42), n_jobs=-1)

    rf_model = RandomForestRegressor(**rf_params).fit(X_train, y_train)
    rp = rf_model.predict(X_test)
    r2_r = r2_score(y_test, rp)
    mae_r = mean_absolute_error(y_test, rp)
    rmse_r = float(np.sqrt(mean_squared_error(y_test, rp)))

    gap = r2_x - r2_r
    if gap > 0.05:
        verdict = f"XGBoost ahead by {gap:.3f} R2 -- boosting finds structure the forest misses."
    elif gap < -0.05:
        verdict = (f"Random Forest ahead by {abs(gap):.3f} R2 -- likely too few sales for "
                   "boosting; it is overfitting.")
    else:
        verdict = (f"Comparable ({gap:+.3f} R2). Ship XGBoost for the monotone constraints, "
                   "not for accuracy.")
    say(f"  XGB R2 {r2_x:.4f} / MAE ${mae_x:,.0f}   |   "
        f"RF R2 {r2_r:.4f} / MAE ${mae_r:,.0f}")
    say(f"  {verdict}")

    # KFold requires 2 <= n_splits <= n_samples. A hardcoded 5 crashes on any
    # sample under 5 rows -- which includes the deploy script's own 3-row
    # smoke test, and any real analysis of a small or niche market. Below 4
    # samples, cross-validation is not meaningful at all (a 2-fold split on
    # 3 rows tells you almost nothing), so it is skipped outright rather than
    # run and reported with false precision.
    if len(X) < 4:
        say(f"  Skipping cross-validation: only {len(X)} usable sales (need at least 4).")
        cv_scores = None
        cv_folds_used = 0
    else:
        cv_folds_used = min(5, len(X))
        cv = KFold(n_splits=cv_folds_used, shuffle=True, random_state=cfg.get("random_state", 42))
        cv_scores = cross_val_score(xgb.XGBRegressor(**params), X, y, cv=cv, scoring="r2")
        if cv_folds_used < 5:
            say(f"  Cross-validation using {cv_folds_used} folds (sample too small for the usual 5).")

    # ------------------------------------------------ full-sample grids
    final_model = xgb.XGBRegressor(**params).fit(X, y)
    log_model = xgb.XGBRegressor(**params).fit(X, np.log(y))

    n_boot = cfg.get("n_bootstrap", 400)
    grid = adjustment_grid(final_model, X, n_boot=n_boot,
                           random_state=cfg.get("random_state", 42))
    ols = LinearRegression().fit(X, y)
    ols_by_feat = dict(zip(X.columns, ols.coef_))
    grid["OLS_Crosscheck"] = [ols_by_feat[f] * s for f, s in zip(grid["Feature"], grid["Step"])]

    pct = percent_grid(log_model, X, float(y.mean()), n_boot=n_boot,
                       random_state=cfg.get("random_state", 42))
    loc_adj = location_effects(final_model, X, baseline_loc)

    perm = permutation_importance(final_model, X_test, y_test, n_repeats=20,
                                  random_state=cfg.get("random_state", 42), scoring="r2")
    perm_df = pd.DataFrame({"Feature": X.columns, "Importance": perm.importances_mean,
                            "Std_Dev": perm.importances_std}) \
        .sort_values("Importance", ascending=False).reset_index(drop=True)

    metadata = {
        "model_version": cfg["model_version"],
        "dataset_name": name,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file": cfg["excel_path"],
        "n_training_sales": int(len(X)),
        "date_min": str(data["close_date"].min().date()),
        "date_max": str(data["close_date"].max().date()),
        "split_date": str(pd.Timestamp(cutoff).date()),
        "anchor_date": meta_bits["anchor_date"],
        "concessions_netted": bool(cfg.get("net_concessions")),
        "mean_price": float(y.mean()),
        "feature_names": list(X.columns),
        "feature_medians": {k: float(v) for k, v in X.median().items()},
        "location_levels": location_levels,
        "baseline_location": baseline_loc,
        "missing_fields": meta_bits["missing_fields"],
        "dropped_features": dropped,
        "monotone_constraints": {c: int(m) for c, m in zip(X.columns, mono)},
        "hyperparameter_tuning": {
            "xgboost": ({"learning_rate": learning_rate, "n_estimators": n_estimators,
                        "validation_mae": tune_val_mae} if tuned_rate is not None
                       else {"tuned": False, "reason": "sample too small"}),
            "random_forest": (tuned_rf_params if tuned_rf_params is not None
                             else {"tuned": False, "reason": "sample too small"}),
        },
        "metrics": {
            "r2_test": float(r2_x), "mae_test": float(mae_x), "rmse_test": float(rmse_x),
            "cv_r2_mean": float(cv_scores.mean()) if cv_scores is not None else None,
            "cv_r2_std": float(cv_scores.std()) if cv_scores is not None else None,
            "cv_folds": cv_folds_used,
        },
        "model_comparison": {
            "xgboost": {"r2": float(r2_x), "mae": float(mae_x), "rmse": float(rmse_x),
                        "mae_pct": float(mae_x / y_test.mean() * 100)},
            "random_forest": {"r2": float(r2_r), "mae": float(mae_r), "rmse": float(rmse_r),
                              "mae_pct": float(mae_r / y_test.mean() * 100)},
            "verdict": verdict,
        },
        "data_diagnostics": diag,
    }

    if write_artifacts:
        out = Path(cfg.get("local_artifacts", "artifacts")) / cfg["model_version"]
        # Refuse to silently overwrite another dataset's artifacts. Reusing
        # "v1" for a second market is the easy mistake here, and its
        # consequence is a deployed API quietly serving the wrong market.
        if out.exists() and any(out.iterdir()):
            existing = out / "metadata.json"
            if existing.exists():
                prior = json.loads(existing.read_text()).get("dataset_name")
                if prior and prior != name:
                    raise FileExistsError(
                        f"{out} already holds artifacts for dataset '{prior}'. "
                        f"Refusing to overwrite them with '{name}'. Give this dataset "
                        f"its own CONFIG['model_version'] (e.g. 'carmel-v1', 'fishers-v1')."
                    )
        out.mkdir(parents=True, exist_ok=True)
        final_model.save_model(out / "model.json")
        log_model.save_model(out / "model_log.json")
        (out / "metadata.json").write_text(json.dumps(metadata, indent=2))

        def rows(df, cols):
            return [{k.lower(): (v.item() if hasattr(v, "item") else v)
                     for k, v in r[cols].items()} for _, r in df.iterrows()]

        (out / "adjustment_grid.json").write_text(json.dumps({
            "dollar": rows(grid, ["Feature", "Unit", "Step", "Adjustment", "CI_Lower_95",
                                  "CI_Upper_95", "Share_Responsive", "Use_In_Grid"]),
            "percent": rows(pct, ["Feature", "Unit", "Step", "Percent_Effect",
                                  "Pct_CI_Lower", "Pct_CI_Upper", "Dollar_At_Mean_Price"]),
            "location": rows(loc_adj, ["Location", "Adjustment_vs_Baseline"]) if len(loc_adj) else [],
        }, indent=2))
        X.to_parquet(out / "reference.parquet", index=False)
        say(f"  Artifacts -> {out.resolve()}")

    return {
        "name": name, "metadata": metadata, "grid": grid, "percent": pct,
        "location": loc_adj, "importance": perm_df, "X": X, "y": y, "data": data,
        "xgb_model": final_model, "rf_model": rf_model, "diagnostics": diag,
    }
