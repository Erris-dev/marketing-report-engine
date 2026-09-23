"""Validation schemas and quarantine.

Rows that break a rule are moved to ``data/quarantine/`` with a human-readable
``reason`` instead of being dropped silently, and the report shows how many there
were. Structural problems (missing columns, wrong types) are not row problems: they
mean the extract itself is broken, so they raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from mre.sources.ga4_bigquery import CHANNELS

GA4_COUNT_COLUMNS = (
    "sessions",
    "users",
    "view_item_sessions",
    "add_to_cart_sessions",
    "begin_checkout_sessions",
    "purchase_sessions",
    "purchases",
    "purchases_missing_revenue",
    "duplicate_purchase_events",
)
# Columns that can never exceed the session count of the same row.
AT_MOST_SESSIONS = (
    "users",
    "view_item_sessions",
    "add_to_cart_sessions",
    "begin_checkout_sessions",
    "purchase_sessions",
    "purchases",
)

REASON_COLUMN = "reason"
_DUPLICATE_CHECK = "multiple_fields_uniqueness"


class StructuralValidationError(ValueError):
    """The table shape is wrong (missing column, bad type); nothing to quarantine."""


@dataclass(frozen=True)
class ValidationResult:
    valid: pd.DataFrame
    quarantined: pd.DataFrame  # original columns plus ``reason``

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined)


def _date_column(start: date, end: date) -> pa.Column:
    return pa.Column(
        "datetime64[ns]",
        pa.Check.in_range(
            pd.Timestamp(start), pd.Timestamp(end), error=f"date outside {start}..{end}"
        ),
        coerce=True,
    )


def _non_negative(dtype: str) -> pa.Column:
    return pa.Column(dtype, pa.Check.ge(0, error="negative value"), coerce=True)


def _at_most_sessions(column: str) -> pa.Check:
    return pa.Check(
        lambda df: df[column] <= df["sessions"],
        name=f"{column}_le_sessions",
        error=f"{column} > sessions",
    )


def ga4_daily_schema(start: date, end: date) -> pa.DataFrameSchema:
    columns: dict[str, pa.Column] = {
        "date": _date_column(start, end),
        "channel": pa.Column(str, pa.Check.isin(CHANNELS, error="unknown channel label")),
        "revenue": _non_negative("float64"),
    }
    columns.update({c: _non_negative("int64") for c in GA4_COUNT_COLUMNS})
    return pa.DataFrameSchema(
        columns,
        checks=[_at_most_sessions(c) for c in AT_MOST_SESSIONS],
        unique=["date", "channel"],
        name="ga4_daily",
    )


def sim_spend_schema(start: date, end: date, paid_channels: list[str]) -> pa.DataFrameSchema:
    return pa.DataFrameSchema(
        {
            "date": _date_column(start, end),
            "channel": pa.Column(str, pa.Check.isin(paid_channels, error="not a paid channel")),
            "spend": _non_negative("float64"),
            "is_simulated": pa.Column(bool, pa.Check.eq(True, error="is_simulated is not true")),
        },
        unique=["date", "channel"],
        name="sim_spend",
    )


def _reason(row: pd.Series) -> str:
    check = str(row["check"])
    if check == _DUPLICATE_CHECK:
        return "duplicate (date, channel)"
    if check == "not_nullable":
        return f"{row['column']}: missing value"
    # Row-level checks span several columns; only column checks get a column prefix.
    if row["schema_context"] == "DataFrameSchema":
        return check
    return f"{row['column']}: {check}"


def validate_and_quarantine(df: pd.DataFrame, schema: pa.DataFrameSchema) -> ValidationResult:
    """Split rows into valid and quarantined (with reasons)."""
    try:
        validated = schema.validate(df, lazy=True)
        return ValidationResult(validated, df.iloc[0:0].assign(**{REASON_COLUMN: ""}))
    except SchemaErrors as err:
        failures = err.failure_cases

    structural = failures[failures["index"].isna()]
    if not structural.empty:
        details = "; ".join(
            f"{r.column}: {r.check} ({r.failure_case})" for r in structural.itertuples()
        )
        raise StructuralValidationError(f"{schema.name}: {details}")

    failures = failures.assign(reason=failures.apply(_reason, axis=1))
    reasons = (
        failures.drop_duplicates(["index", "reason"])
        .groupby("index")["reason"]
        .agg(lambda r: "; ".join(sorted(r)))
    )
    bad_index = df.index.isin(reasons.index)
    quarantined = df[bad_index].assign(**{REASON_COLUMN: reasons})
    # Re-validate the clean rows so they come back with coerced types.
    valid = schema.validate(df[~bad_index])
    return ValidationResult(valid, quarantined)


def write_quarantine(result: ValidationResult, name: str, directory: Path) -> Path:
    """Always write the file (possibly empty) so a stale quarantine never lingers."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.csv"
    result.quarantined.to_csv(path, index=False)
    return path
