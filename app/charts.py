"""
Matplotlib chart generation for the market analysis PDF.

Charts render with the non-interactive 'Agg' backend -- there is no display
in a Lambda container, and Agg has to be selected before pyplot is imported
or the import can fail depending on what's on the system. Every function
returns PNG bytes, ready to embed as a ReportLab Image flowable. Nothing is
written to disk, which matters on Lambda's read-only filesystem outside /tmp.

These mirror the exploratory and diagnostic plots from the Phase 1 notebook
and the original regression template, so a batch job triggered by an S3
upload produces the same visual evidence a human running the notebook by
hand would see -- not just a table of numbers.
"""

from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 11,
    "axes.titleweight": "bold",
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
})


def _fig_to_png(fig, dpi: int = 140) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def price_distribution_chart(data: pd.DataFrame) -> bytes:
    """Histogram of price/sqft + scatter of price vs GLA, side by side.
    Same pairing as Phase 1 notebook Step 7."""
    fig, ax = plt.subplots(1, 2, figsize=(9.5, 3.6))

    ppsf = data["ppsf"].dropna()
    n_bins = min(25, max(3, len(ppsf) // 2 or 1))
    ax[0].hist(ppsf, bins=n_bins, color="#3498db", edgecolor="black", alpha=0.8)
    if len(ppsf):
        med = ppsf.median()
        ax[0].axvline(med, color="red", linestyle="--", lw=1.6,
                      label=f"Median ${med:,.0f}")
        ax[0].legend(fontsize=8)
    ax[0].set_xlabel("Price per Sq Ft ($)")
    ax[0].set_ylabel("Number of Sales")
    ax[0].set_title("Distribution of Price per Sq Ft")

    ax[1].scatter(data["gla_sqft"], data["sale_price"], alpha=0.6,
                  color="#2980b9", edgecolor="black", linewidth=0.3, s=28)
    ax[1].set_xlabel("Above-Grade GLA (sq ft)")
    ax[1].set_ylabel("Sale Price ($)")
    ax[1].set_title("Sale Price vs Living Area")

    fig.tight_layout()
    return _fig_to_png(fig)


def characteristics_grid_chart(data: pd.DataFrame) -> bytes:
    """2x3 grid: bedrooms / full baths / garage box plots on top, age / lot
    / market-conditions scatter on the bottom. Matches the original
    regression template's exploratory layout."""
    fig, ax = plt.subplots(2, 3, figsize=(11, 6.4))

    def _no_data(a, label):
        """A characteristic that is missing or was dropped for having no
        variation still gets its panel -- but showing an empty axes with
        default 0-1 limits looks like a rendering bug, not like 'this
        export doesn't have this field.' Say so directly instead."""
        a.text(0.5, 0.5, "Not available\nin this export", ha="center", va="center",
              fontsize=10, color="#888888", transform=a.transAxes)
        a.set_xticks([])
        a.set_yticks([])
        a.set_title(f"Price by {label}")

    def _box(a, col, label, color):
        if col not in data.columns or data[col].notna().sum() == 0:
            _no_data(a, label)
            return
        present = data[data[col].notna()]
        groups, ticks = [], []
        for level, g in present.groupby(col):
            if len(g):
                groups.append(g["sale_price"].values)
                ticks.append(str(int(level)))
        if not groups:
            _no_data(a, label)
            return
        bp = a.boxplot(groups, tick_labels=ticks, patch_artist=True)
        for box in bp["boxes"]:
            box.set_facecolor(color)
            box.set_alpha(0.75)
        a.set_xlabel(label)
        a.set_ylabel("sale_price")
        a.set_title(f"Price by {label}")

    def _scatter(a, col, label, xlabel, color, trend=False):
        if col not in data.columns or data[col].notna().sum() == 0:
            _no_data(a, label)
            return
        sub = data[[col, "sale_price"]].dropna()
        x = sub[col].astype(float).values
        y = sub["sale_price"].astype(float).values
        a.scatter(x, y, alpha=0.55, color=color, s=24)
        if trend and len(x) >= 2 and np.ptp(x) > 0:
            coef = np.polyfit(x, y, 1)
            xs = np.linspace(x.min(), x.max(), 20)
            a.plot(xs, np.polyval(coef, xs), "r--", lw=1.6,
                  label=f"Trend: ${coef[0]:,.0f}/month")
            a.legend(fontsize=8)
        a.set_xlabel(xlabel)
        a.set_ylabel("Sale Price ($)")
        a.set_title(label)

    _box(ax[0, 0], "bedrooms", "Bedroom Count", "#3498db")
    _box(ax[0, 1], "baths_full", "Full Bathroom Count", "#2ecc71")
    _box(ax[0, 2], "garage_spaces", "Garage Spaces", "#e67e22")

    _scatter(ax[1, 0], "age_at_sale", "Price vs Age", "Age at Sale (years)", "#9b59b6")
    _scatter(ax[1, 1], "lot_sqft", "Price vs Lot Size", "Lot Size (sq ft)", "#1abc9c")
    _scatter(ax[1, 2], "months_since_start", "Market Conditions Over Time",
            "Months Since First Sale", "#e74c3c", trend=True)

    fig.tight_layout()
    return _fig_to_png(fig)


def correlation_heatmap_chart(X: pd.DataFrame, y: pd.Series) -> bytes:
    """Correlation matrix of price and numeric characteristics. Plain
    matplotlib rather than seaborn -- the container already carries
    xgboost, sklearn, and pandas, and seaborn adds real image size for
    one chart it does not meaningfully improve on imshow + text."""
    numeric_cols = [c for c in X.columns if not c.startswith("loc_")]
    corr_df = pd.concat([y.rename("sale_price"), X[numeric_cols]], axis=1).corr()

    fig, ax = plt.subplots(figsize=(7.5, 6.5))
    im = ax.imshow(corr_df.values, cmap="coolwarm", vmin=-1, vmax=1, aspect="auto")
    ax.set_xticks(range(len(corr_df.columns)))
    ax.set_xticklabels(corr_df.columns, rotation=90, fontsize=7.5)
    ax.set_yticks(range(len(corr_df.columns)))
    ax.set_yticklabels(corr_df.columns, fontsize=7.5)
    for i in range(len(corr_df)):
        for j in range(len(corr_df)):
            v = corr_df.values[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    fontsize=6.5, color="white" if abs(v) > 0.55 else "black")
    fig.colorbar(im, ax=ax, shrink=0.8)
    ax.set_title("Correlation Matrix")
    fig.tight_layout()
    return _fig_to_png(fig)


def prediction_diagnostics_chart(y_test, xgb_pred, rf_pred) -> bytes:
    """
    Actual-vs-predicted and residual plots for both models on the held-out
    later period. This is the regression equivalent of what a ROC curve
    does for a classifier -- a visual read on how well the model
    discriminates -- but ROC/AUC themselves do not apply here: they are
    defined for a binary yes/no outcome scored by a threshold-varying
    probability, and this system predicts a continuous dollar amount.
    There is no threshold to sweep and no positive/negative class to score.

    A perfect model would put every point in the top row on the dashed
    diagonal. The bottom row should look like structureless noise centered
    on zero; a visible slope, curve, or funnel shape there means the model
    is systematically off in some price range rather than merely imprecise
    everywhere -- a materially different, more specific finding than the
    single R-squared/MAE numbers in the table above convey.
    """
    y_test = np.asarray(y_test, dtype=float)
    fig, ax = plt.subplots(2, 2, figsize=(10, 8))

    for col, (name, pred, color) in enumerate([
        ("XGBoost", np.asarray(xgb_pred, dtype=float), "#2E86C1"),
        ("Random Forest", np.asarray(rf_pred, dtype=float), "#27AE60"),
    ]):
        ax[0, col].scatter(y_test, pred, alpha=0.6, color=color, s=24,
                          edgecolor="black", linewidth=0.3)
        if len(y_test) >= 2:
            lo = float(min(y_test.min(), pred.min()))
            hi = float(max(y_test.max(), pred.max()))
            ax[0, col].plot([lo, hi], [lo, hi], "k--", lw=1.3, label="Perfect prediction")
            ax[0, col].legend(fontsize=7.5)
        ax[0, col].set_xlabel("Actual Sale Price ($)")
        ax[0, col].set_ylabel("Predicted Sale Price ($)")
        ax[0, col].set_title(f"{name}: Actual vs Predicted")

        resid = y_test - pred
        ax[1, col].scatter(pred, resid, alpha=0.6, color=color, s=24,
                          edgecolor="black", linewidth=0.3)
        ax[1, col].axhline(0, color="black", lw=1.2, linestyle="--")
        ax[1, col].set_xlabel("Predicted Sale Price ($)")
        ax[1, col].set_ylabel("Residual (Actual - Predicted, $)")
        ax[1, col].set_title(f"{name}: Residuals")

    fig.tight_layout()
    return _fig_to_png(fig)


def permutation_importance_chart(perm_df: pd.DataFrame) -> bytes:
    """Horizontal bar chart with std-dev error bars, teal -- matches the
    Phase 1 notebook's Step 12 styling exactly."""
    fig, ax = plt.subplots(figsize=(8.5, 5))
    df = perm_df.sort_values("Importance", ascending=True)
    err = df["Std_Dev"] if "Std_Dev" in df.columns else None
    ax.barh(df["Feature"], df["Importance"], xerr=err,
           color="#16A085", edgecolor="black", capsize=3)
    ax.axvline(0, color="black", lw=1)
    ax.set_xlabel("Decrease in R-squared When Shuffled")
    ax.set_title("Relative Importance of Property Characteristics")
    fig.tight_layout()
    return _fig_to_png(fig)


def model_comparison_chart(cmp_: dict) -> bytes:
    """Simple two-bar R-squared comparison, XGBoost vs Random Forest.
    NaN-safe: a sample small enough to produce an undefined R-squared
    (e.g. a 1-row test split) renders as a labeled 'n/a' bar rather than
    crashing the chart."""
    fig, ax = plt.subplots(figsize=(6, 4.2))
    xgb_r2 = cmp_.get("xgboost", {}).get("r2")
    rf_r2 = cmp_.get("random_forest", {}).get("r2")

    def _clean(v):
        return 0.0 if v is None or (isinstance(v, float) and np.isnan(v)) else v

    labels = ["XGBoost", "Random Forest"]
    values = [_clean(xgb_r2), _clean(rf_r2)]
    colors = ["#2E86C1", "#27AE60"]
    bars = ax.bar(labels, values, color=colors, edgecolor="black")
    for bar, raw in zip(bars, [xgb_r2, rf_r2]):
        is_nan = raw is None or (isinstance(raw, float) and np.isnan(raw))
        label = "n/a" if is_nan else f"{raw:.4f}"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(),
               label, ha="center", va="bottom", fontweight="bold")
    ax.set_ylabel("Test R-squared")
    ax.set_title("Model Comparison: Predictive Accuracy on Held-Out Sales")
    fig.tight_layout()
    return _fig_to_png(fig)
