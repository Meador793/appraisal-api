"""
Run the same pipeline over several MLS exports and compare them side by side.

    python scripts/compare_datasets.py

Each dataset gets its OWN model, its own artifacts, and its own adjustment
grid. Nothing is pooled. The point is to see whether two markets price a garage
bay differently, which combining them into one model would erase.

Edit DATASETS below. Everything except the file path, the version, and the
column mapping is shared through BASE.

--------------------------------------------------------------------------
A caution about the comparison table this prints
--------------------------------------------------------------------------
R-squared is NOT comparable across datasets. It is the share of price variation
explained, so a market with wide price dispersion can score higher than a
tightly-banded one with an objectively better model. If you pulled one export
across all price points and another inside a $400-600k band, the first will
look better and the difference tells you about the pulls, not the models.

Compare these instead:
  - MAE as a percentage of mean price -- scale-free, comparable across markets
  - The XGB-minus-RF gap WITHIN each dataset -- that comparison is like-for-like
  - Whether the same characteristics come out supported in each market

The table below prints all three, with R-squared last and flagged.
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pipeline import run_dataset

# ==========================================================================
# Shared settings. Anything not dataset-specific belongs here, once.
# ==========================================================================
BASE = {
    "sheet_name": "Sheet1",
    "status_col": "Status",
    "status_keep": "Sold",
    "target": "Close Price",
    "net_concessions": True,
    "min_location_count": 15,
    "min_level_count": 10,
    "random_state": 42,
    "n_bootstrap": 300,
    "train_through": None,
    "test_fraction": 0.20,
    "monotone": {
        "gla_sqft": 1, "bsmt_fin_sqft": 1, "bedrooms": 0, "baths_full": 1,
        "baths_half": 1, "garage_spaces": 1, "fireplaces": 1, "lot_sqft": 1,
        "age_at_sale": -1, "months_since_start": 1,
    },
    "local_artifacts": "artifacts",
    "aws_region": "us-east-2",
}

STANDARD_COLS = {
    "main_sqft": "Main Level SqFt",
    "upper_sqft": "Upper SqFt",
    "bsmt_fin_sqft": "Apprx Below Grade Fin SqFt",
    "bedrooms": "Bedrooms",
    "baths_full": "Baths Full",
    "baths_half": "Baths Half",
    "garage_spaces": "Garage Spaces",
    "fireplaces": "Fireplaces",
    "lot_sqft": "Lot Size SqFt",
    "year_built": "Year Built",
    "close_date": "Close Date",
    "location": "Area",
    "concessions": "Concessions",
}

# ==========================================================================
# The datasets. Give each its OWN model_version -- reusing "v1" would
# overwrite the previous dataset's artifacts (the pipeline refuses, but give
# them distinct names anyway so the S3 prefixes stay separate too).
# ==========================================================================
DATASETS = [
    {
        **BASE,
        "dataset_name": "Carmel",
        "excel_path": "CARMEL_Excel_Fixed.xlsx",
        "model_version": "carmel-v1",
        "cols": STANDARD_COLS,
    },
    {
        **BASE,
        "dataset_name": "Westfield",
        "excel_path": "WESTFIELD_Excel.xlsx",
        "model_version": "westfield-v1",
        # Almost identical columns -- override only what differs, and set any
        # field this export lacks to None. The pipeline drops it and records
        # that it was absent rather than valuing it at zero.
        "cols": {**STANDARD_COLS,
                 "bsmt_fin_sqft": "Below Grade Finished SF",
                 "fireplaces": None},
    },
    {
        **BASE,
        "dataset_name": "Fishers",
        "excel_path": "FISHERS_Excel.xlsx",
        "model_version": "fishers-v1",
        "cols": STANDARD_COLS,
    },
]


def main():
    results, failed = [], []

    for cfg in DATASETS:
        try:
            results.append(run_dataset(cfg))
        except FileNotFoundError as exc:
            # One missing export should not abandon the whole comparison.
            print(f"\n  SKIPPED {cfg['dataset_name']}: {exc}")
            failed.append(cfg["dataset_name"])
        except Exception as exc:
            print(f"\n  FAILED {cfg['dataset_name']}: {type(exc).__name__}: {exc}")
            failed.append(cfg["dataset_name"])

    if not results:
        print("\nNo datasets ran. Check the excel_path values in DATASETS.")
        return

    # ------------------------------------------------------- model comparison
    rows = []
    for r in results:
        m = r["metadata"]
        c = m["model_comparison"]
        rows.append({
            "Dataset": r["name"],
            "Sales": m["n_training_sales"],
            "Mean price": f"${m['mean_price']:,.0f}",
            "XGB MAE %": f"{c['xgboost']['mae_pct']:.2f}%",
            "RF MAE %": f"{c['random_forest']['mae_pct']:.2f}%",
            "XGB-RF R2 gap": f"{c['xgboost']['r2'] - c['random_forest']['r2']:+.3f}",
            "XGB R2 *": f"{c['xgboost']['r2']:.3f}",
        })

    print("\n" + "=" * 96)
    print("MODEL COMPARISON ACROSS DATASETS")
    print("=" * 96)
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n* R-squared is NOT comparable across datasets -- it depends on each")
    print("  sample's price dispersion. Read 'XGB MAE %' to compare markets, and")
    print("  'XGB-RF R2 gap' to compare the two models within a market.")

    # ------------------------------------------------ adjustments side by side
    print("\n" + "=" * 96)
    print("ADJUSTMENTS BY MARKET (blank = not supported, or absent from that export)")
    print("=" * 96)

    wide = None
    for r in results:
        g = r["grid"][["Unit", "Adjustment", "Use_In_Grid"]].copy()
        g[r["name"]] = [
            f"${a:,.0f}" if v.startswith("YES") else ""
            for a, v in zip(g["Adjustment"], g["Use_In_Grid"])
        ]
        g = g[["Unit", r["name"]]]
        wide = g if wide is None else wide.merge(g, on="Unit", how="outer")

    print(wide.fillna("").to_string(index=False))
    print("\nA row where one market shows a number and another is blank is the")
    print("interesting case: either the markets genuinely differ, or one export")
    print("lacks the variation to estimate it. The per-dataset output above tells")
    print("you which -- check 'Fields absent' and the range-restriction flags.")

    # ----------------------------------------------------------- diagnostics
    print("\n" + "=" * 96)
    print("DATA ADEQUACY")
    print("=" * 96)
    for r in results:
        d, m = r["diagnostics"], r["metadata"]
        status = "RANGE RESTRICTED" if d["flags"] else "ok"
        print(f"  {r['name']:<14} span {d['price_range_ratio']:>5.2f}x  "
              f"CV {d['price_cv']:.3f}  price-GLA r {d['gla_corr']:.3f}   [{status}]")
        if m["missing_fields"]:
            print(f"                 fields absent: {', '.join(m['missing_fields'])}")
        if m["dropped_features"]:
            print(f"                 dropped, no variation: {', '.join(m['dropped_features'])}")

    if failed:
        print(f"\nDid not run: {', '.join(failed)}")

    print("\nEach dataset wrote its own artifacts under artifacts/<version>/.")
    print("To deploy one, point MODEL_PREFIX at that version's S3 prefix.")


if __name__ == "__main__":
    main()
