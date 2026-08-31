"""
PDF report generation.

Produces a work-file-quality document: indicated value, the market-derived
adjustment grid with confidence intervals, the model comparison, the paired
scenario analysis, and an explicit limitations page.

Built in memory with ReportLab and returned as bytes. Nothing is written to
the container filesystem -- Fargate tasks are ephemeral, so a file written to
local disk vanishes on the next deployment and cannot be served on a retry.
Any PDF worth keeping goes to S3 (see /report?persist=true in main.py).

Note on fonts: ReportLab's built-in fonts have no glyphs for Unicode
subscript, superscript, or fancy dashes -- they render as solid black boxes.
Everything here is plain ASCII, and the one place that needs a superscript
uses ReportLab's <super> markup instead.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageBreak,
                                PageTemplate, Paragraph, Spacer, Table, TableStyle)

NAVY = colors.HexColor("#1F3864")
SLATE = colors.HexColor("#44546A")
LIGHT = colors.HexColor("#EDF1F7")
GREEN = colors.HexColor("#1E7B47")
RED = colors.HexColor("#B3261E")
AMBER = colors.HexColor("#8A6100")


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("TitleBig", parent=ss["Title"], fontSize=19, textColor=NAVY,
                          spaceAfter=2, alignment=TA_LEFT))
    ss.add(ParagraphStyle("SubTitle", parent=ss["Normal"], fontSize=10.5, textColor=SLATE,
                          spaceAfter=14))
    ss.add(ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, textColor=NAVY,
                          spaceBefore=16, spaceAfter=6))
    ss.add(ParagraphStyle("H3", parent=ss["Heading3"], fontSize=11, textColor=SLATE,
                          spaceBefore=10, spaceAfter=4))
    ss.add(ParagraphStyle("Body", parent=ss["Normal"], fontSize=9.3, leading=13.2,
                          spaceAfter=7))
    ss.add(ParagraphStyle("Small", parent=ss["Normal"], fontSize=8, leading=10.5,
                          textColor=SLATE))
    ss.add(ParagraphStyle("ValueBig", parent=ss["Normal"], fontSize=26, textColor=NAVY,
                          leading=30))
    return ss


def _money(v, dp=0):
    """Negatives render as -$1,806, not $-1,806. Appraisal grids carry negative
    adjustments routinely (age, and often bedrooms), so this is not a rare path."""
    try:
        return f"-${abs(v):,.{dp}f}" if v < 0 else f"${v:,.{dp}f}"
    except (TypeError, ValueError):
        return "n/a"


def _table(data, widths, align_right=None, header=True, font_size=8.2):
    align_right = align_right or []
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    style = [
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#D5DBE5")),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ]
    for col in align_right:
        style.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _verdict_color(text: str):
    t = (text or "").upper()
    if t.startswith("YES"):
        return GREEN
    if t.startswith("WEAK"):
        return AMBER
    return RED


class _Doc(BaseDocTemplate):
    """Adds the running footer with page numbers and the model version, so a
    printed page found on a desk still identifies which model produced it."""

    def __init__(self, buf, version, generated, **kw):
        super().__init__(buf, pagesize=letter,
                         leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                         topMargin=0.65 * inch, bottomMargin=0.7 * inch, **kw)
        self.version = version
        self.generated = generated
        frame = Frame(self.leftMargin, self.bottomMargin,
                      self.width, self.height, id="body")
        self.addPageTemplates([PageTemplate(id="main", frames=[frame],
                                            onPage=self._decorate)])

    def _decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#C9D2E0"))
        canvas.setLineWidth(0.5)
        canvas.line(doc.leftMargin, 0.58 * inch, doc.pagesize[0] - doc.rightMargin, 0.58 * inch)
        canvas.setFont("Helvetica", 7.2)
        canvas.setFillColor(SLATE)
        canvas.drawString(doc.leftMargin, 0.42 * inch,
                          f"Model {self.version}  |  generated {self.generated}  |  "
                          "statistical support tool, not an appraisal")
        canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 0.42 * inch,
                               f"Page {canvas.getPageNumber()}")
        canvas.restoreState()


def build_report(payload: dict, subject: dict, meta: dict,
                 title: str = "Market-Derived Adjustment Analysis") -> bytes:
    """
    payload : the same dict /predict returns
    subject : the raw subject property as submitted
    meta    : metadata.json contents
    """
    ss = _styles()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buf = io.BytesIO()
    doc = _Doc(buf, payload.get("model_version", "unknown"), generated)
    S = []

    # ---------------------------------------------------------------- header
    S.append(Paragraph(title, ss["TitleBig"]))
    ident = []
    if subject.get("address"):
        ident.append(subject["address"])
    if subject.get("file_number"):
        ident.append(f"File {subject['file_number']}")
    ident.append(f"Effective date {subject.get('effective_date') or 'not specified'}")
    S.append(Paragraph("  |  ".join(str(i) for i in ident), ss["SubTitle"]))

    # ------------------------------------------------------- indicated value
    lo, hi = payload["value_range_low"], payload["value_range_high"]
    vbox = Table([[
        Paragraph("INDICATED VALUE", ss["Small"]),
        Paragraph("SUPPORTED RANGE", ss["Small"]),
    ], [
        Paragraph(_money(payload["indicated_value"]), ss["ValueBig"]),
        Paragraph(f"{_money(lo)} to {_money(hi)}", ss["Body"]),
    ]], colWidths=[3.3 * inch, 3.7 * inch])
    vbox.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.7, NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
    ]))
    S.append(vbox)
    S.append(Spacer(1, 5))
    S.append(Paragraph(
        "The supported range is the model's mean absolute error on held-out sales, applied "
        "either side of the point estimate. It describes typical predictive error on this "
        "market, not a confidence interval on this particular property.", ss["Small"]))

    # ------------------------------------------------------------- warnings
    if payload.get("warnings"):
        S.append(Spacer(1, 10))
        rows = [["Item", "Note"]]
        for w in payload["warnings"]:
            rows.append(["Caution", Paragraph(w, ss["Small"])])
        t = _table(rows, [0.85 * inch, 6.15 * inch])
        t.setStyle(TableStyle([("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#FDF3E3")),
                               ("TEXTCOLOR", (0, 1), (0, -1), AMBER),
                               ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold")]))
        S.append(t)

    # ------------------------------------------------- subject characteristics
    S.append(Paragraph("Subject property as valued", ss["H2"]))
    defaulted = set(payload.get("defaulted_fields", []))
    feats = payload.get("features_used", {})
    pretty = {
        "gla_sqft": "Above-grade GLA (sq ft)", "bsmt_fin_sqft": "Finished basement (sq ft)",
        "bedrooms": "Bedrooms", "baths_full": "Full baths", "baths_half": "Half baths",
        "garage_spaces": "Garage bays", "fireplaces": "Fireplaces",
        "lot_sqft": "Lot size (sq ft)", "age_at_sale": "Age at effective date (yrs)",
        "months_since_start": "Months from market anchor",
    }
    rows = [["Characteristic", "Value", "Source"]]
    for k, label in pretty.items():
        if k not in feats:
            continue
        rows.append([label, f"{feats[k]:,.0f}",
                     "sample median (not supplied)" if k in defaulted else "supplied"])
    rows.append(["Location", payload.get("location_used", "-"),
                 "matched" if payload.get("location_matched") else "baseline (not matched)"])
    S.append(_table(rows, [2.7 * inch, 1.5 * inch, 2.8 * inch], align_right=[1]))
    if defaulted:
        S.append(Paragraph(
            "Fields marked 'sample median' were not supplied and were filled with the median "
            "of the training sample. They contribute to the indicated value. Supply them for "
            "a property-specific result.", ss["Small"]))

    # ------------------------------------------------------- adjustment grid
    grid = payload.get("adjustment_grid") or []
    if grid:
        S.append(PageBreak())
        S.append(Paragraph("Market-derived adjustment grid", ss["H2"]))
        S.append(Paragraph(
            "Each figure is the average change in sale price associated with one additional "
            "unit of that characteristic, holding every other characteristic constant. It is "
            "estimated by taking every closed sale in the sample, adding one unit of the "
            "characteristic, re-predicting, and averaging the difference. This is the "
            "gradient-boosted equivalent of a regression coefficient, estimated without "
            "assuming the effect is the same for every property.", ss["Body"]))
        S.append(Paragraph(
            "<b>Decision rule.</b> Where the 95 percent interval spans zero, the sample has "
            "not shown the effect differs from no effect. Do not carry that line into a grid "
            "regardless of how reasonable its sign appears. 'Responds' is the share of sales "
            "where the model's prediction moved at all; a low share means the average is being "
            "carried by a minority of records.", ss["Body"]))

        rows = [["Characteristic", "Adjustment", "95% interval", "Responds", "Use in grid"]]
        for r in grid:
            rows.append([
                r["unit"],
                _money(r["adjustment"]),
                f"{_money(r['ci_lower_95'])} to {_money(r['ci_upper_95'])}",
                f"{r['share_responsive']*100:.0f}%",
                Paragraph(f"<font color='#{_verdict_color(r['use_in_grid']).hexval()[2:]}'>"
                          f"{r['use_in_grid']}</font>", ss["Small"]),
            ])
        S.append(_table(rows, [2.15 * inch, 0.95 * inch, 1.75 * inch, 0.65 * inch, 1.5 * inch],
                        align_right=[1, 3]))

    # ---------------------------------------------------- location adjustments
    locs = payload.get("location_adjustments") or []
    if locs:
        S.append(Paragraph("Location adjustments", ss["H3"]))
        S.append(Paragraph(
            f"Dollar difference relative to the baseline location "
            f"({meta.get('baseline_location', 'baseline')}), other characteristics held constant.",
            ss["Body"]))
        rows = [["Location", "Adjustment vs baseline"]]
        for r in locs:
            rows.append([r["location"], _money(r["adjustment_vs_baseline"])])
        S.append(_table(rows, [3.2 * inch, 2.2 * inch], align_right=[1]))

    # ------------------------------------------------------ percentage grid
    pct = payload.get("percent_grid") or []
    if pct:
        S.append(Paragraph("Percentage adjustments", ss["H2"]))
        S.append(Paragraph(
            "From a second model trained on the natural log of price. A percentage effect is "
            "usually more stable across price tiers than a fixed dollar amount, which is the "
            "standard objection to a dollar grid: a full bath is not worth the same in a "
            "$300,000 house and a $900,000 house. Close agreement between the dollar column "
            "here and the grid above is good evidence the adjustment is real.", ss["Body"]))
        rows = [["Characteristic", "Percent effect", "95% interval", "Dollars at mean price"]]
        for r in pct:
            rows.append([
                r["unit"], f"{r['percent_effect']:.2f}%",
                f"{r['pct_ci_lower']:.2f}% to {r['pct_ci_upper']:.2f}%",
                _money(r["dollar_at_mean_price"]),
            ])
        S.append(_table(rows, [2.5 * inch, 1.15 * inch, 1.85 * inch, 1.5 * inch],
                        align_right=[1, 3]))

    # -------------------------------------------------------- contributions
    contribs = payload.get("contributions") or []
    if contribs:
        S.append(PageBreak())
        S.append(Paragraph("What drives this subject's value", ss["H2"]))
        S.append(Paragraph(
            "Each line is how much the indicated value would change if that one characteristic "
            "were reset to the sample median, everything else unchanged. These do not sum to "
            "the total: a gradient-boosted model is not additive, so the parts depend on the "
            "order you remove them in. Read them as magnitudes, not as an accounting.", ss["Body"]))
        rows = [["Characteristic", "Subject", "Sample median", "Contribution"]]
        for r in contribs:
            rows.append([
                pretty.get(r["feature"], r["feature"].replace("loc_", "Location: ")),
                f"{r['subject_value']:,.0f}", f"{r['sample_median']:,.0f}",
                _money(r["contribution"]),
            ])
        S.append(_table(rows, [2.6 * inch, 1.2 * inch, 1.4 * inch, 1.4 * inch],
                        align_right=[1, 2, 3]))

    # ------------------------------------------------------------ scenarios
    scen = payload.get("scenarios") or {}
    if scen:
        S.append(Paragraph("Paired scenario analysis", ss["H2"]))
        S.append(Paragraph(
            "Identical properties differing in exactly one characteristic. This is the "
            "matched-pairs analysis an appraiser does with comparable sales, except everything "
            "else is held constant by construction rather than by hoping two comparables happen "
            "to be otherwise identical.", ss["Body"]))
        for feature, rows_in in scen.items():
            block = [Paragraph(pretty.get(feature, feature), ss["H3"])]
            rows = [[pretty.get(feature, feature), "Indicated value", "Step change"]]
            for r in rows_in:
                diff = r.get("difference")
                rows.append([f"{r['value']:,.0f}", _money(r["indicated_value"]),
                             "-" if diff is None else _money(diff)])
            block.append(_table(rows, [1.9 * inch, 1.7 * inch, 1.5 * inch], align_right=[0, 1, 2]))
            S.append(KeepTogether(block))

    # ---------------------------------------------------------- model basis
    S.append(PageBreak())
    S.append(Paragraph("Basis of the analysis", ss["H2"]))
    cmp_ = meta.get("model_comparison", {})
    metrics = meta.get("metrics", {})
    diag = meta.get("data_diagnostics", {})

    rows = [["Item", "Value"]]
    rows += [
        ["Model version", payload.get("model_version", "-")],
        ["Trained", meta.get("trained_at", "-")],
        ["Closed sales analyzed", f"{meta.get('n_training_sales', 0):,}"],
        ["Sale date range", f"{meta.get('date_min', '?')} to {meta.get('date_max', '?')}"],
        ["Mean sale price", _money(meta.get("mean_price"))],
        ["Median price per sq ft", _money(diag.get("raw_median_ppsf"), 2)],
        ["Concessions netted from price", "yes" if meta.get("concessions_netted") else "no"],
    ]
    S.append(_table(rows, [2.6 * inch, 4.4 * inch]))

    if cmp_:
        S.append(Paragraph("Model selection: XGBoost against Random Forest", ss["H3"]))
        S.append(Paragraph(
            "Both are tree ensembles and neither assumes a fixed dollar adjustment per unit. "
            "They are compared on the same held-out sales. A large gap in favour of either "
            "means the other is leaving structure on the table; a small gap means the choice "
            "is not doing much work and the simpler, more stable model is preferable.",
            ss["Body"]))
        rows = [["Metric (held-out sales)", "XGBoost", "Random Forest"]]
        for label, key, fmt in [
            ("R-squared", "r2", "{:.4f}"),
            ("Mean absolute error", "mae", "money"),
            ("Root mean squared error", "rmse", "money"),
            ("MAE as % of mean price", "mae_pct", "{:.2f}%"),
        ]:
            x = cmp_.get("xgboost", {}).get(key)
            r = cmp_.get("random_forest", {}).get(key)
            f = (lambda v: _money(v)) if fmt == "money" else (lambda v: fmt.format(v) if v is not None else "-")
            rows.append([label, f(x) if x is not None else "-", f(r) if r is not None else "-"])
        S.append(_table(rows, [2.8 * inch, 1.6 * inch, 1.6 * inch], align_right=[1, 2]))
        if cmp_.get("verdict"):
            S.append(Paragraph(f"<b>Selection:</b> {cmp_['verdict']}", ss["Body"]))

    if metrics.get("cv_r2_mean") is not None:
        S.append(Paragraph(
            f"Cross-validated R-squared across {metrics.get('cv_folds', 5)} folds: "
            f"{metrics['cv_r2_mean']:.4f} (standard deviation {metrics.get('cv_r2_std', 0):.4f}). "
            "Cross-validation is a more stable accuracy estimate than a single split, which "
            "matters on samples of a few hundred sales.", ss["Body"]))

    # ------------------------------------------------------- data adequacy
    if diag:
        S.append(Paragraph("Data adequacy", ss["H3"]))
        S.append(Paragraph(
            "Regression and tree models alike can only measure a price effect where the sample "
            "contains variation in price and in the characteristic. These are the checks that "
            "decide which questions this particular export can answer.", ss["Body"]))
        rows = [["Check", "Value", "Reading"]]
        rows.append(["Price span (max / min)", f"{diag.get('price_range_ratio', 0):.2f}x",
                     "Below 2x indicates a filtered price band"])
        rows.append(["Coefficient of variation", f"{diag.get('price_cv', 0):.3f}",
                     "Below 0.20 indicates a filtered price band"])
        rows.append(["Correlation, price to GLA", f"{diag.get('gla_corr', 0):.3f}",
                     "0.70 to 0.90 in an unrestricted sample"])
        S.append(_table(rows, [2.1 * inch, 1.2 * inch, 3.7 * inch], align_right=[1]))
        for flag in diag.get("flags", []):
            S.append(Paragraph(f"<b>Range restriction:</b> {flag}", ss["Small"]))
        if diag.get("flags"):
            S.append(Paragraph(
                "Every adjustment above is biased toward zero when the sample is restricted on "
                "price. No modelling technique repairs this. The fix is a wider MLS export.",
                ss["Body"]))

    # ---------------------------------------------------------- limitations
    S.append(Paragraph("Limitations", ss["H2"]))
    for text in [
        "<b>The average and marginal rates are not interchangeable.</b> Price divided by size "
        "includes the land, the site improvements, and every amenity spread across the square "
        "footage. The grid above reports the marginal rate: what the market pays for one "
        "additional square foot with everything else unchanged. The marginal rate is almost "
        "always lower, and it is the correct basis for a size adjustment between comparables.",

        "<b>A near-zero or negative bedroom figure is a finding, not a failure.</b> Once square "
        "footage is in the model, the bedroom effect measures what the market pays for "
        "partitioning the same space into more rooms. That is often close to zero and sometimes "
        "negative, because extra bedrooms come at the expense of room size.",

        "<b>Tree models do not extrapolate.</b> A subject outside the range of the training "
        "sales is valued at the edge of that range, not beyond it. The response flags this "
        "where it applies, but a subject that is unusual on several characteristics at once "
        "should be treated with more caution than any single flag conveys.",

        "<b>What the model cannot see.</b> Condition, quality of finish, recent updates, view, "
        "and functional utility are not in this MLS export. They are frequently the largest "
        "single differences between two comparables, and none of them is in these numbers.",

        "<b>This output is a statistical support tool.</b> It is market evidence for an "
        "adjustment, not a value opinion, and it does not satisfy USPAP on its own. A qualified "
        "appraiser's judgement decides what enters a report.",
    ]:
        S.append(Paragraph(text, ss["Body"]))

    doc.build(S)
    return buf.getvalue()


# ==========================================================================
# Market analysis report -- the output of an S3 analysis job.
#
# The subject-property report above answers "what is this house worth". This
# one answers "what does this market pay for a bathroom", which is the
# deliverable when you drop a new MLS export into the jobs bucket. No subject
# property is involved.
# ==========================================================================

def build_market_report(meta: dict, grid, pct, loc, importance) -> bytes:
    ss = _styles()
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    buf = io.BytesIO()
    doc = _Doc(buf, meta.get("model_version", "unknown"), generated)
    S = []
    d = meta.get("data_diagnostics", {})
    cmp_ = meta.get("model_comparison", {})

    S.append(Paragraph("Market-Derived Adjustment Analysis", ss["TitleBig"]))
    S.append(Paragraph(
        f"{meta.get('dataset_name', 'Market')}  |  {meta.get('n_training_sales', 0):,} closed sales  "
        f"|  {meta.get('date_min')} to {meta.get('date_max')}", ss["SubTitle"]))

    # ------------------------------------------------------------- headline
    supported = [r for r in grid.to_dict("records") if str(r["Use_In_Grid"]).startswith("YES")]
    box = Table([[
        Paragraph("SALES ANALYZED", ss["Small"]),
        Paragraph("MEAN SALE PRICE", ss["Small"]),
        Paragraph("SUPPORTED ADJUSTMENTS", ss["Small"]),
    ], [
        Paragraph(f"{meta.get('n_training_sales', 0):,}", ss["ValueBig"]),
        Paragraph(_money(meta.get("mean_price")), ss["ValueBig"]),
        Paragraph(f"{len(supported)} of {len(grid)}", ss["ValueBig"]),
    ]], colWidths=[2.33 * inch, 2.33 * inch, 2.34 * inch])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.7, NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
    ]))
    S.append(box)

    # --------------------------------------------------- range restriction
    if d.get("flags"):
        S.append(Spacer(1, 10))
        rows = [["", "Data adequacy warning"]]
        for f in d["flags"]:
            rows.append(["CAUTION", Paragraph(f, ss["Small"])])
        rows.append(["", Paragraph(
            "Every adjustment below is biased toward zero when the sample is restricted on "
            "price. No modelling technique repairs this. The fix is a wider MLS export.",
            ss["Small"])])
        t = _table(rows, [0.85 * inch, 6.15 * inch])
        t.setStyle(TableStyle([("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#FDF3E3")),
                               ("TEXTCOLOR", (0, 1), (0, -1), AMBER),
                               ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold")]))
        S.append(t)

    if meta.get("missing_fields"):
        S.append(Paragraph(
            f"<b>Not present in this export:</b> {', '.join(meta['missing_fields'])}. "
            "These characteristics were excluded from the model rather than valued at zero. "
            "The analysis is silent on them; it does not conclude they are worthless.",
            ss["Body"]))

    # -------------------------------------------------------------- grid
    S.append(Paragraph("Adjustment grid", ss["H2"]))
    S.append(Paragraph(
        "Each figure is the average change in sale price associated with one additional unit "
        "of that characteristic, holding every other characteristic constant. Where the 95 "
        "percent interval spans zero, the sample has not shown the effect differs from no "
        "effect -- do not carry that line into a grid regardless of how reasonable its sign "
        "appears. 'OLS' is the same adjustment from an ordinary least squares fit: close "
        "agreement means the number is robust, wide divergence means it is either nonlinear "
        "or unstable.", ss["Body"]))

    rows = [["Characteristic", "Adjustment", "95% interval", "OLS", "Use in grid"]]
    for r in grid.to_dict("records"):
        rows.append([
            r["Unit"], _money(r["Adjustment"]),
            f"{_money(r['CI_Lower_95'])} to {_money(r['CI_Upper_95'])}",
            _money(r.get("OLS_Crosscheck")),
            Paragraph(f"<font color='#{_verdict_color(r['Use_In_Grid']).hexval()[2:]}'>"
                      f"{r['Use_In_Grid']}</font>", ss["Small"]),
        ])
    S.append(_table(rows, [2.0 * inch, 0.95 * inch, 1.6 * inch, 0.85 * inch, 1.6 * inch],
                    align_right=[1, 2, 3]))

    # ---------------------------------------------------------- location
    if len(loc):
        S.append(Paragraph("Location adjustments", ss["H3"]))
        S.append(Paragraph(
            f"Dollar difference relative to the baseline location "
            f"({meta.get('baseline_location')}), everything else held constant.", ss["Body"]))
        rows = [["Location", "Adjustment vs baseline"]]
        for r in loc.to_dict("records"):
            rows.append([r["Location"], _money(r["Adjustment_vs_Baseline"])])
        S.append(_table(rows, [3.2 * inch, 2.2 * inch], align_right=[1]))

    # ----------------------------------------------------------- percent
    S.append(PageBreak())
    S.append(Paragraph("Percentage adjustments", ss["H2"]))
    S.append(Paragraph(
        "From a second model trained on the natural log of price. A percentage effect is "
        "usually more stable across price tiers than a fixed dollar amount, which is the "
        "standard objection to a dollar grid: a full bath is not worth the same in a "
        "$300,000 house and a $900,000 house.", ss["Body"]))
    rows = [["Characteristic", "Percent effect", "95% interval", "Dollars at mean price"]]
    for r in pct.to_dict("records"):
        rows.append([r["Unit"], f"{r['Percent_Effect']:.2f}%",
                     f"{r['Pct_CI_Lower']:.2f}% to {r['Pct_CI_Upper']:.2f}%",
                     _money(r["Dollar_At_Mean_Price"])])
    S.append(_table(rows, [2.5 * inch, 1.15 * inch, 1.85 * inch, 1.5 * inch],
                    align_right=[1, 3]))

    # -------------------------------------------------------- importance
    S.append(Paragraph("Relative importance", ss["H2"]))
    S.append(Paragraph(
        "Drop in R-squared when each characteristic is randomly shuffled on the held-out "
        "sales. Read this for ranking; read the grid for the dollar figure. A characteristic "
        "can rank high on one and low on the other.", ss["Body"]))
    rows = [["Characteristic", "Importance"]]
    for r in importance.head(12).to_dict("records"):
        rows.append([r["Feature"], f"{r['Importance']:.4f}"])
    S.append(_table(rows, [3.2 * inch, 1.5 * inch], align_right=[1]))

    # --------------------------------------------------- model selection
    S.append(Paragraph("Model selection: XGBoost against Random Forest", ss["H2"]))
    S.append(Paragraph(
        "Both are tree ensembles and neither assumes a fixed dollar adjustment per unit. They "
        "are compared on the same held-out later period. R-squared here is not comparable to "
        "another market's, because it depends on that sample's price dispersion; compare MAE "
        "as a percentage of mean price across markets instead.", ss["Body"]))
    rows = [["Metric (held-out sales)", "XGBoost", "Random Forest"]]
    for label, key, fmt in [("R-squared", "r2", "{:.4f}"), ("Mean absolute error", "mae", "money"),
                            ("Root mean squared error", "rmse", "money"),
                            ("MAE as % of mean price", "mae_pct", "{:.2f}%")]:
        x = cmp_.get("xgboost", {}).get(key)
        rr = cmp_.get("random_forest", {}).get(key)
        f = (lambda v: _money(v)) if fmt == "money" else (lambda v: fmt.format(v))
        rows.append([label, f(x) if x is not None else "-", f(rr) if rr is not None else "-"])
    S.append(_table(rows, [2.8 * inch, 1.6 * inch, 1.6 * inch], align_right=[1, 2]))
    if cmp_.get("verdict"):
        S.append(Paragraph(f"<b>Selection:</b> {cmp_['verdict']}", ss["Body"]))

    m = meta.get("metrics", {})
    if m.get("cv_r2_mean") is not None:
        S.append(Paragraph(
            f"Cross-validated R-squared across {m.get('cv_folds', 5)} folds: "
            f"{m['cv_r2_mean']:.4f} (standard deviation {m.get('cv_r2_std', 0):.4f}).",
            ss["Body"]))

    # -------------------------------------------------------------- basis
    S.append(Paragraph("Basis and limitations", ss["H2"]))
    rows = [["Item", "Value"]]
    rows += [
        ["Source file", str(meta.get("source_file", "-"))],
        ["Model version", meta.get("model_version", "-")],
        ["Trained (UTC)", meta.get("trained_at", "-")],
        ["Time split date", meta.get("split_date", "-")],
        ["Median price per sq ft", _money(d.get("raw_median_ppsf"), 2)],
        ["Price span (max/min)", f"{d.get('price_range_ratio', 0):.2f}x"],
        ["Correlation, price to GLA", f"{d.get('gla_corr', 0):.3f}"],
        ["Concessions netted", "yes" if meta.get("concessions_netted") else "no"],
    ]
    S.append(_table(rows, [2.6 * inch, 4.4 * inch]))

    for text in [
        "<b>The average and marginal rates are not interchangeable.</b> Price divided by size "
        "includes the land, the site improvements, and every amenity spread across the square "
        "footage. This grid reports the marginal rate: what the market pays for one additional "
        "square foot with everything else unchanged. It is almost always lower, and it is the "
        "correct basis for a size adjustment between comparables.",

        "<b>A near-zero or negative bedroom figure is a finding, not a failure.</b> Once square "
        "footage is in the model, the bedroom effect measures what the market pays for "
        "partitioning the same space into more rooms.",

        "<b>What the model cannot see.</b> Condition, quality of finish, recent updates, view, "
        "and functional utility are not in an MLS export. They are frequently the largest single "
        "differences between two comparables, and none of them is in these numbers.",

        "<b>This output is a statistical support tool.</b> It is market evidence for an "
        "adjustment, not a value opinion, and it does not satisfy USPAP on its own.",
    ]:
        S.append(Paragraph(text, ss["Body"]))

    doc.build(S)
    return buf.getvalue()
