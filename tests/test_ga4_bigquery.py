import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pytest

from mre.config import SourceConfig
from mre.sources import ga4_bigquery as ga4


@pytest.mark.parametrize(
    ("source", "medium", "channel"),
    [
        ("google", "cpc", "paid_search"),
        ("google", "CPC", "paid_search"),
        ("google", "organic", "organic_search"),
        (None, "organic", "organic_search"),
        ("<Other>", "referral", "referral"),
        ("shop.googlemerchandisestore.com", "referral", "referral"),
        ("(direct)", "(none)", "direct"),
        ("Partners", "affiliate", "other"),
        ("Newsletter_January_2021", "email", "other"),
        (None, None, "unknown"),
        ("<Other>", "<Other>", "unknown"),
        ("(data deleted)", "(data deleted)", "unknown"),
        ("<Other>", "(data deleted)", "unknown"),
        ("x", "NULL", "unknown"),
        ("x", "", "unknown"),
    ],
)
def test_channel_mapping(source: str | None, medium: str | None, channel: str) -> None:
    # Runs the exact SQL snippet used in BigQuery, so the test cannot drift from it.
    sql = f"SELECT {ga4.channel_case_sql()} AS channel FROM (SELECT ? AS source, ? AS medium)"
    row = duckdb.execute(sql, [source, medium]).fetchone()
    assert row is not None
    assert row[0] == channel


def test_sql_mapping_only_produces_known_channels() -> None:
    # Keeps the SQL mapping and the validation schema's channel list in sync.
    labels = set(re.findall(r"(?:THEN|ELSE)\s+'([a-z_]+)'", ga4.channel_case_sql()))
    assert labels == set(ga4.CHANNELS)


def test_render_sql_fills_template_and_keeps_date_filter() -> None:
    sql = ga4.render_sql("proj.ds.events_*")
    assert "$" not in sql
    assert "`proj.ds.events_*`" in sql
    assert "_TABLE_SUFFIX BETWEEN @start_suffix AND @end_suffix" in sql
    assert "'paid_search'" in sql


@dataclass
class FakeJob:
    total_bytes_processed: int
    table: pa.Table

    def result(self) -> "FakeJob":
        return self

    def to_arrow(self, create_bqstorage_client: bool = True) -> pa.Table:
        return self.table


@dataclass
class FakeClient:
    bytes_processed: int
    table: pa.Table
    calls: list[bool] = field(default_factory=list)

    def query(self, sql: str, job_config: Any) -> FakeJob:
        self.calls.append(job_config.dry_run)
        params = {p.name: p.value for p in job_config.query_parameters}
        assert params == {"start_suffix": "20201101", "end_suffix": "20210131"}
        assert job_config.maximum_bytes_billed == 5_000_000_000
        return FakeJob(self.bytes_processed, self.table)


def _table(columns: tuple[str, ...] = ga4.EXPECTED_COLUMNS) -> pa.Table:
    data: dict[str, list[Any]] = {c: [1] for c in columns}
    if "date" in data:
        data["date"] = [date(2020, 11, 1)]
    if "channel" in data:
        data["channel"] = ["direct"]
    return pa.table(data)


def _cfg(tmp_path: Path) -> SourceConfig:
    return SourceConfig(raw_path=tmp_path / "raw" / "ga4_daily.parquet")


def test_extract_dry_runs_first_then_writes_parquet(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    client = FakeClient(bytes_processed=1_000, table=_table())
    ga4.extract(client, cfg)
    assert client.calls == [True, False]
    assert cfg.raw_path.exists()
    assert list(pd.read_parquet(cfg.raw_path).columns) == list(ga4.EXPECTED_COLUMNS)


def test_extract_refuses_when_over_cap(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    client = FakeClient(bytes_processed=6_000_000_000, table=_table())
    with pytest.raises(ga4.QueryTooLargeError):
        ga4.extract(client, cfg)
    assert client.calls == [True]  # the real query never ran
    assert not cfg.raw_path.exists()


def test_extract_rejects_missing_columns(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    client = FakeClient(bytes_processed=1_000, table=_table(("date", "channel")))
    with pytest.raises(RuntimeError, match="missing columns"):
        ga4.extract(client, cfg)


def test_make_client_requires_project() -> None:
    with pytest.raises(RuntimeError, match="GCP_PROJECT"):
        ga4.make_client(None)


def test_profile_reports_coverage_duplicates_and_shares(tmp_path: Path) -> None:
    cfg = SourceConfig(start_date=date(2020, 11, 1), end_date=date(2020, 11, 3))
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-11-01", "2020-11-01", "2020-11-02", "2020-11-02"]),
            "channel": ["direct", "unknown", "direct", "direct"],
            "sessions": [30, 10, 40, 20],
            "revenue": [50.0, 0.0, 50.0, 0.0],
            "purchases": [1, 0, 1, 0],
            "purchases_missing_revenue": [0, 1, 0, 0],
            "duplicate_purchase_events": [0, 0, 2, 0],
        }
    )
    text = ga4.profile(df, cfg)
    assert "duplicate (date, channel) rows: 1" in text
    assert "(2 of 3 expected days present)" in text
    assert "purchases missing revenue 1" in text
    # direct: 90 of 100 sessions, all revenue
    rows = {line.split()[0]: line.split()[1:] for line in text.splitlines() if line}
    assert rows["direct"] == ["90", "90.0%", "100.0%"]
    assert rows["unknown"] == ["10", "10.0%", "0.0%"]
