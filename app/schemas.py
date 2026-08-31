"""
Request and response contracts.

Pydantic validation is what makes bad input return a 422 with a readable
message instead of a 500 from deep inside pandas. That difference matters a
lot to whoever is integrating against this API -- a 422 tells them which
field is wrong; a 500 tells them nothing.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SubjectProperty(BaseModel):
    """Subject property characteristics. Every field is optional: anything you
    omit falls back to the training sample's median, and the response tells
    you which fields were defaulted so nothing is silently invented."""

    model_config = {"json_schema_extra": {"examples": [{
        "main_sqft": 1800, "upper_sqft": 900, "bsmt_fin_sqft": 800,
        "bedrooms": 4, "baths_full": 3, "baths_half": 1,
        "garage_spaces": 3, "fireplaces": 1, "lot_sqft": 12000,
        "year_built": 2005, "location": "Zionsville",
        "effective_date": "2026-08-30",
        "address": "1234 Example Ln, Carmel IN 46032",
    }]}}

    # Size
    main_sqft: float | None = Field(None, ge=0, le=20000, description="Main level above-grade sq ft")
    upper_sqft: float | None = Field(None, ge=0, le=20000, description="Upper level above-grade sq ft")
    gla_sqft: float | None = Field(None, ge=0, le=40000, description="Total above-grade GLA; overrides main+upper if given")
    bsmt_fin_sqft: float | None = Field(None, ge=0, le=20000, description="Finished below-grade sq ft")
    lot_sqft: float | None = Field(None, ge=0, le=5_000_000)

    # Rooms
    bedrooms: float | None = Field(None, ge=0, le=20)
    baths_full: float | None = Field(None, ge=0, le=20)
    baths_half: float | None = Field(None, ge=0, le=20)

    # Amenities
    garage_spaces: float | None = Field(None, ge=0, le=20)
    fireplaces: float | None = Field(None, ge=0, le=20)

    # Age and time
    year_built: int | None = Field(None, ge=1700, le=2100)
    age_at_sale: float | None = Field(None, ge=0, le=300, description="Overrides year_built if given")
    effective_date: date | None = Field(None, description="Effective date of the appraisal; drives the market-conditions adjustment")

    # Location
    location: str | None = Field(None, max_length=120, description="Must match a location level from training, otherwise the baseline is used and the response says so")

    # Report identification only — never used as a model feature
    address: str | None = Field(None, max_length=250)
    file_number: str | None = Field(None, max_length=60)
    client_name: str | None = Field(None, max_length=120)
    appraiser_name: str | None = Field(None, max_length=120)

    @model_validator(mode="after")
    def _at_least_one_size(self):
        if self.gla_sqft is None and self.main_sqft is None and self.upper_sqft is None:
            raise ValueError(
                "Provide gla_sqft, or main_sqft and/or upper_sqft. Valuing a "
                "subject on the sample median size is not a meaningful appraisal input."
            )
        return self


class ScenarioRequest(BaseModel):
    feature: str = Field(..., description="Feature to vary, e.g. 'garage_spaces'")
    values: list[float] = Field(..., min_length=2, max_length=12)


class PredictRequest(BaseModel):
    subject: SubjectProperty
    include_grid: bool = Field(True, description="Include the market adjustment grid")
    include_percent_grid: bool = Field(True, description="Include percentage adjustments from the log-price model")
    include_contributions: bool = Field(True, description="Per-feature contribution vs the median property")
    scenarios: list[ScenarioRequest] | None = Field(
        None, description="Paired scenario analysis: vary one characteristic, hold everything else constant"
    )


class ReportRequest(PredictRequest):
    """Same payload as /predict. The response is a PDF instead of JSON."""
    report_title: str = Field("Market-Derived Adjustment Analysis", max_length=140)
    delivery: Literal["attachment", "inline"] = Field(
        "attachment",
        description="attachment triggers a browser download; inline renders in an embedded viewer",
    )


class AdjustmentRow(BaseModel):
    feature: str
    unit: str
    step: float
    adjustment: float
    ci_lower_95: float
    ci_upper_95: float
    share_responsive: float
    use_in_grid: str


class ContributionRow(BaseModel):
    feature: str
    subject_value: float
    sample_median: float
    contribution: float


class PredictResponse(BaseModel):
    indicated_value: float
    value_range_low: float
    value_range_high: float
    model_version: str
    location_used: str
    location_matched: bool
    defaulted_fields: list[str]
    features_used: dict
    adjustment_grid: list[AdjustmentRow] | None = None
    percent_grid: list[dict] | None = None
    location_adjustments: list[dict] | None = None
    contributions: list[ContributionRow] | None = None
    scenarios: dict | None = None
    warnings: list[str] = []


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    model_version: str | None = None
    detail: str | None = None


class ModelInfoResponse(BaseModel):
    model_version: str
    trained_at: str | None = None
    n_training_sales: int | None = None
    feature_names: list[str]
    location_levels: list[str] = []
    baseline_location: str | None = None
    metrics: dict = {}
    model_comparison: dict = {}
    data_diagnostics: dict = {}
