"""Extract the GA4 sample from BigQuery into a local Parquet cache.

BigQuery is queried once: every run is dry-run first and the real query carries a
``maximum_bytes_billed`` cap, so a mistake fails instead of costing money. Normal
report runs only read the Parquet file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from string import Template
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import bigquery

from mre.config import SourceConfig

SQL_DIR = Path(__file__).resolve().parents[3] / "sql"

EXPECTED_COLUMNS = (
    "date",
    "channel",
    "sessions",
    "users",
    "view_item_sessions",
    "add_to_cart_sessions",
    "begin_checkout_sessions",
    "purchase_sessions",
    "purchases",
    "revenue",
    "purchases_missing_revenue",
    "duplicate_purchase_events",
)

GB = 1_000_000_000


class QueryTooLargeError(RuntimeError):
    """The dry run says the query would scan more than the configured cap."""


@dataclass(frozen=True)
class DryRunResult:
    bytes_processed: int
    cap_bytes: int

    @property
    def within_cap(self) -> bool:
        return self.bytes_processed <= self.cap_bytes

    def describe(self) -> str:
        return (
            f"Dry run: {self.bytes_processed / GB:.2f} GB would be scanned "
            f"(cap {self.cap_bytes / GB:.2f} GB, sandbox free tier 1000 GB/month)."
        )


def channel_case_sql() -> str:
    return (SQL_DIR / "channel_case.sql").read_text(encoding="utf-8").strip()


def render_sql(table: str) -> str:
    """Fill in table name and channel mapping; dates stay query parameters."""
    template = Template((SQL_DIR / "ga4_daily.sql").read_text(encoding="utf-8"))
    return template.substitute(table=table, channel_case=channel_case_sql())


def _suffix(d: date) -> str:
    return d.strftime("%Y%m%d")


def _job_config(cfg: SourceConfig, *, dry_run: bool) -> bigquery.QueryJobConfig:
    return bigquery.QueryJobConfig(
        dry_run=dry_run,
        use_query_cache=not dry_run,
        maximum_bytes_billed=int(cfg.max_bytes_billed_gb * GB),
        query_parameters=[
            bigquery.ScalarQueryParameter("start_suffix", "STRING", _suffix(cfg.start_date)),
            bigquery.ScalarQueryParameter("end_suffix", "STRING", _suffix(cfg.end_date)),
        ],
    )


def make_client(project: str | None) -> Any:
    if not project:
        raise RuntimeError("GCP_PROJECT is not set; add it to .env (see .env.example).")
    return bigquery.Client(project=project)


def dry_run(client: Any, cfg: SourceConfig) -> DryRunResult:
    job = client.query(render_sql(cfg.table), job_config=_job_config(cfg, dry_run=True))
    return DryRunResult(int(job.total_bytes_processed), int(cfg.max_bytes_billed_gb * GB))


def extract(client: Any, cfg: SourceConfig) -> pa.Table:
    """Run the extract and write Parquet. Always dry-runs first."""
    check = dry_run(client, cfg)
    if not check.within_cap:
        raise QueryTooLargeError(check.describe())
    job = client.query(render_sql(cfg.table), job_config=_job_config(cfg, dry_run=False))
    # REST download is plenty for ~600 rows and avoids the extra Storage API dependency.
    table: pa.Table = job.result().to_arrow(create_bqstorage_client=False)
    missing = set(EXPECTED_COLUMNS) - set(table.column_names)
    if missing:
        raise RuntimeError(f"Extract is missing columns: {sorted(missing)}")
    cfg.raw_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, cfg.raw_path)
    return table


def load_raw(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def profile(df: pd.DataFrame, cfg: SourceConfig) -> str:
    """Short human-readable profile used to sanity-check an extract."""
    days = df["date"].dt.date
    expected_days = (cfg.end_date - cfg.start_date).days + 1
    dupes = int(df.duplicated(["date", "channel"]).sum())
    lines = [
        f"rows: {len(df)}  channels: {df['channel'].nunique()}  "
        f"duplicate (date, channel) rows: {dupes}",
        f"dates: {days.min()} to {days.max()}  "
        f"({days.nunique()} of {expected_days} expected days present)",
        f"totals: sessions {int(df['sessions'].sum()):,}  purchases {int(df['purchases'].sum()):,}"
        f"  revenue {df['revenue'].sum():,.2f}",
        f"data quality: purchases missing revenue {int(df['purchases_missing_revenue'].sum()):,}"
        f"  duplicate purchase events {int(df['duplicate_purchase_events'].sum()):,}",
        "",
        "null rates:",
    ]
    lines += [f"  {col}: {rate:.1%}" for col, rate in df.isna().mean().items()]
    by_channel = (
        df.groupby("channel")[["sessions", "revenue"]]
        .sum()
        .sort_values("sessions", ascending=False)
    )
    session_share = by_channel["sessions"] / by_channel["sessions"].sum()
    revenue_share = by_channel["revenue"] / by_channel["revenue"].sum()
    lines += ["", f"{'channel':16s}{'sessions':>10s}{'share':>8s}{'revenue share':>15s}"]
    for channel, sessions, s_share, r_share in zip(
        by_channel.index, by_channel["sessions"], session_share, revenue_share, strict=True
    ):
        lines.append(f"{channel!s:16s}{int(sessions):>10,}{s_share:>8.1%}{r_share:>15.1%}")
    return "\n".join(lines)
