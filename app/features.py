"""
Feature engineering — THE single source of truth.

This module is imported by BOTH the Phase 1 training notebook and the Phase 2
FastAPI service. It is deliberately not copy-pasted. If the notebook and the
API ever disagree about what `gla_sqft` means or what order the columns are in,
XGBoost will not raise an error -- it will silently return wrong dollar values.
That failure mode is called training/serving skew and it is the single most
common production ML bug.

Two entry points:

  build_training_frame(df_sold, cfg)  -> used ONCE, in the notebook
  build_serving_row(subject, meta)    -> used on EVERY /predict call

Both funnel through the same private primitives below, so the definitions
cannot drift apart.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Primitives — every derived variable is defined exactly once, right here
# --------------------------------------------------------------------------

# The order here IS the model's feature order for the numeric block.
# metadata.json records the final full order (numerics + location dummies);
# at serving time we trust metadata.json, never this list.
NUMERIC_FEATURES = [
    "gla_sqft",
    "bsmt_fin_sqft",
    "bedrooms",
    "baths_full",
    "baths_half",
    "garage_spaces",
    "fireplaces",
    "lot_sqft",
    "age_at_sale",
    "months_since_start",
]

# Which CONFIG["cols"] entries feed which model feature. Used to report, per
# dataset, which characteristics the export could not support.
NUMERIC_FEATURE_SOURCES = {
    "main_sqft", "upper_sqft", "bsmt_fin_sqft", "bedrooms", "baths_full",
    "baths_half", "garage_spaces", "fireplaces", "lot_sqft", "year_built",
}

# Human-readable units, used by the adjustment grid and the PDF report
UNIT_LABELS = {
    "gla_sqft": "per sq ft of above-grade GLA",
    "bsmt_fin_sqft": "per sq ft of finished basement",
    "bedrooms": "per bedroom",
    "baths_full": "per full bathroom",
    "baths_half": "per half bathroom",
    "garage_spaces": "per garage bay",
    "fireplaces": "per fireplace",
    "lot_sqft": "per sq ft of lot",
    "age_at_sale": "per year of age",
    "months_since_start": "per month (market conditions)",
}

# The natural increment for each feature when we measure a marginal effect.
# One square foot of GLA is a meaningless unit to an appraiser, so the grid
# reports per-100-sqft for area variables and rescales back where useful.
DEFAULT_STEPS = {
    "gla_sqft": 100.0,
    "bsmt_fin_sqft": 100.0,
    "lot_sqft": 1000.0,
    "bedrooms": 1.0,
    "baths_full": 1.0,
    "baths_half": 1.0,
    "garage_spaces": 1.0,
    "fireplaces": 1.0,
    "age_at_sale": 1.0,
    "months_since_start": 1.0,
}

STEP_LABELS = {
    "gla_sqft": "per 100 sq ft of above-grade GLA",
    "bsmt_fin_sqft": "per 100 sq ft of finished basement",
    "lot_sqft": "per 1,000 sq ft of lot",
}


def to_num(series) -> pd.Series:
    """Coerce to numeric. MLS exports store numbers as text constantly
    (Garage Spaces arrives as the string '2'), so errors='coerce' turns
    anything unparseable into NaN rather than raising mid-pipeline."""
    return pd.to_numeric(series, errors="coerce")


def _col(df: pd.DataFrame, name):
    """
    Fetch a mapped column, tolerating the two cases that come up constantly
    when you reuse this pipeline on a second MLS export:

        cols["fireplaces"] = None      the field does not exist in this market
        cols["fireplaces"] = "Fplc"    mapped, but this export actually lacks it

    Both return an all-NaN column instead of raising. The caller records the
    field as missing, and build_design_matrix then DROPS it from the model
    rather than feeding in a column of zeros. That distinction matters: a
    constant column is not "a characteristic the market pays nothing for", it
    is "a question this dataset cannot answer", and an adjustment grid must
    never make those two look the same.
    """
    if name is None or name not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return df[name]


def _gla(main_sqft, upper_sqft):
    """Above-grade GLA = main + upper. Deliberately NOT 'Total Finished SqFt',
    which blends finished basement into the same number and forces one rate
    onto two kinds of space that the market prices differently."""
    return np.nan_to_num(main_sqft, nan=0.0) + np.nan_to_num(upper_sqft, nan=0.0)


def _age_at_sale(year_built, close_date):
    """Age relative to each sale's own closing date, not to today.
    Interpretable directly as a depreciation rate in dollars per year."""
    return pd.to_datetime(close_date).dt.year - year_built


def _months_since(close_date, anchor):
    """Months elapsed since the first sale in the sample. This is the market
    conditions / time adjustment variable."""
    d = pd.to_datetime(close_date)
    a = pd.Timestamp(anchor)
    return (d.dt.year - a.year) * 12 + (d.dt.month - a.month)


# --------------------------------------------------------------------------
# TRAINING PATH — notebook only
# --------------------------------------------------------------------------

def build_training_frame(df_sold: pd.DataFrame, cfg: dict):
    """
    Turn a filtered MLS export into modeling variables.

    Returns
    -------
    data : DataFrame of engineered variables (one row per closed sale)
    meta_bits : dict of things the API will need later (anchor date, etc.)
    """
    c = cfg["cols"]
    data = pd.DataFrame(index=df_sold.index)

    # --- TARGET: cash-equivalent sale price -------------------------------
    data["sale_price"] = to_num(_col(df_sold, cfg["target"]))
    concessions_netted = 0
    if cfg.get("net_concessions") and c.get("concessions"):
        conc = to_num(_col(df_sold, c.get("concessions"))).fillna(0)
        data["sale_price"] = data["sale_price"] - conc
        concessions_netted = int((conc > 0).sum())

    # --- SIZE: above-grade and below-grade kept as separate rates ---------
    data["gla_sqft"] = _gla(
        to_num(_col(df_sold, c.get("main_sqft"))).values,
        to_num(_col(df_sold, c.get("upper_sqft"))).values,
    )
    data["bsmt_fin_sqft"] = to_num(_col(df_sold, c.get("bsmt_fin_sqft"))).fillna(0)

    # --- ROOM COUNT: full and half baths stay separate --------------------
    data["bedrooms"] = to_num(_col(df_sold, c.get("bedrooms")))
    data["baths_full"] = to_num(_col(df_sold, c.get("baths_full")))
    data["baths_half"] = to_num(_col(df_sold, c.get("baths_half"))).fillna(0)

    # --- AMENITIES: a blank garage/fireplace field means none -------------
    data["garage_spaces"] = to_num(_col(df_sold, c.get("garage_spaces"))).fillna(0)
    data["fireplaces"] = to_num(_col(df_sold, c.get("fireplaces"))).fillna(0)
    data["lot_sqft"] = to_num(_col(df_sold, c.get("lot_sqft")))

    # --- TIME --------------------------------------------------------------
    close_date = pd.to_datetime(_col(df_sold, c.get("close_date")), errors="coerce")
    year_built = to_num(_col(df_sold, c.get("year_built")))
    data["age_at_sale"] = _age_at_sale(year_built, close_date)
    anchor = close_date.min()
    data["months_since_start"] = _months_since(close_date, anchor)
    data["close_date"] = close_date

    # --- LOCATION ----------------------------------------------------------
    data["location"] = (
        _col(df_sold, c.get("location")).astype(str).str.strip()
        if c.get("location") and c["location"] in df_sold.columns else "ALL"
    )

    data = data.dropna(subset=["sale_price", "gla_sqft"])
    data = data[data["gla_sqft"] > 0]
    data["ppsf"] = data["sale_price"] / data["gla_sqft"]

    # Which mapped fields this export actually lacked. build_design_matrix uses
    # this to drop them, and the metadata records it so a reviewer can see that
    # the model was silent on a characteristic rather than valuing it at zero.
    missing_fields = sorted(
        generic for generic, mls in c.items()
        if generic in NUMERIC_FEATURE_SOURCES
        and (mls is None or mls not in df_sold.columns)
    )

    meta_bits = {
        "anchor_date": str(anchor.date()) if pd.notna(anchor) else None,
        "concessions_netted": concessions_netted,
        "missing_fields": missing_fields,
    }
    return data, meta_bits


def build_design_matrix(data: pd.DataFrame, min_location_count: int = 15):
    """One-hot the location and assemble X. drop_first holds one location out
    as the baseline, so every loc_ coefficient reads as a dollar difference
    relative to that baseline -- the form a location adjustment takes."""
    loc_counts = data["location"].value_counts()
    keep = loc_counts[loc_counts >= min_location_count].index
    grouped = np.where(data["location"].isin(keep), data["location"], "Other")
    data = data.assign(location_grouped=grouped)

    X = data[NUMERIC_FEATURES].copy()

    # Drop characteristics with no variation in THIS dataset. A column that is
    # entirely zero (the field was missing) or entirely one value (every sale
    # has a 2-car garage) cannot support an adjustment. Left in, XGBoost simply
    # never splits on it and the grid reports roughly $0 -- which reads as "the
    # market pays nothing for a garage" when the truth is "this export cannot
    # tell you". Dropping it forces that distinction to be visible.
    dropped = [f for f in NUMERIC_FEATURES if X[f].nunique(dropna=True) <= 1]
    if dropped:
        X = X.drop(columns=dropped)

    dummies = pd.get_dummies(
        data["location_grouped"], prefix="loc", drop_first=True, dtype=int
    )
    X = pd.concat([X, dummies], axis=1)

    baseline = sorted(pd.unique(grouped))[0]
    y = data["sale_price"]
    X = X.astype(float)
    X.attrs["dropped_features"] = dropped
    return X, y, baseline, sorted(pd.unique(grouped))


# --------------------------------------------------------------------------
# SERVING PATH — API only. Must produce a row identical in shape and ORDER
# to what build_design_matrix produced during training.
# --------------------------------------------------------------------------

def build_serving_row(subject: dict, meta: dict) -> pd.DataFrame:
    """
    Build a one-row feature frame for a subject property.

    subject : raw appraiser input (main_sqft, upper_sqft, bedrooms, ...).
              Anything omitted falls back to the training-sample median,
              which is recorded in metadata.json.
    meta    : the metadata.json dict loaded at API startup.

    The reindex at the end is the anti-skew guarantee: columns come out in
    metadata's recorded order, and any location the caller didn't name
    becomes a 0.
    """
    medians = meta["feature_medians"]
    row = dict(medians)  # start from typical values

    # Derived size, computed with the SAME primitive used in training
    if subject.get("main_sqft") is not None or subject.get("upper_sqft") is not None:
        row["gla_sqft"] = float(
            _gla(
                np.array([subject.get("main_sqft") or 0.0]),
                np.array([subject.get("upper_sqft") or 0.0]),
            )[0]
        )
    if subject.get("gla_sqft") is not None:
        row["gla_sqft"] = float(subject["gla_sqft"])

    # Straight pass-through numerics
    for f in ["bsmt_fin_sqft", "bedrooms", "baths_full", "baths_half",
              "garage_spaces", "fireplaces", "lot_sqft"]:
        if subject.get(f) is not None:
            row[f] = float(subject[f])

    # Age: accept either an explicit age or a year_built + effective date
    if subject.get("age_at_sale") is not None:
        row["age_at_sale"] = float(subject["age_at_sale"])
    elif subject.get("year_built") is not None:
        eff = pd.Timestamp(subject.get("effective_date") or pd.Timestamp.today())
        row["age_at_sale"] = float(eff.year - subject["year_built"])

    # Market conditions: months from the training anchor to the effective date
    if subject.get("months_since_start") is not None:
        row["months_since_start"] = float(subject["months_since_start"])
    elif subject.get("effective_date") and meta.get("anchor_date"):
        eff = pd.Timestamp(subject["effective_date"])
        a = pd.Timestamp(meta["anchor_date"])
        row["months_since_start"] = float(
            (eff.year - a.year) * 12 + (eff.month - a.month)
        )

    # Location one-hot. Unknown or baseline location => all dummies stay 0.
    for col in meta["feature_names"]:
        if col.startswith("loc_"):
            row[col] = 0.0
    if subject.get("location"):
        key = f"loc_{str(subject['location']).strip()}"
        if key in meta["feature_names"]:
            row[key] = 1.0

    frame = pd.DataFrame([row])
    # THE line that prevents skew: exact training order, missing -> 0.0
    frame = frame.reindex(columns=meta["feature_names"], fill_value=0.0)
    return frame.astype(float)


def resolve_location(subject_location, meta) -> tuple[str, bool]:
    """Report back which location bucket was actually used, so the PDF can
    say so plainly instead of silently valuing the subject in the baseline."""
    if not subject_location:
        return meta.get("baseline_location", "baseline"), False
    key = f"loc_{str(subject_location).strip()}"
    if key in meta["feature_names"]:
        return str(subject_location).strip(), True
    return meta.get("baseline_location", "baseline"), False
