"""Command-line entry point (``mre``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
import yaml

from mre import __version__, schemas
from mre.config import DEFAULT_CONFIG_PATH, load_config, load_secrets
from mre.sources import ga4_bigquery as ga4
from mre.sources import sim_spend as sim

app = typer.Typer(
    help="Marketing Report Engine: GA4 e-commerce data to a weekly marketing PDF.",
    no_args_is_help=True,
)

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Path to config.yaml.", exists=True, dir_okay=False)
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Marketing Report Engine."""


@app.command()
def extract(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only report bytes scanned; costs nothing.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-query BigQuery even if the cache exists.")
    ] = False,
) -> None:
    """Extract GA4 daily channel data from BigQuery into the Parquet cache."""
    cfg = load_config(config).source
    if cfg.raw_path.exists() and not (dry_run or force):
        typer.echo(f"Cache exists at {cfg.raw_path}; not querying BigQuery (use --force).")
        typer.echo(ga4.profile(ga4.load_raw(cfg.raw_path), cfg))
        return
    client = ga4.make_client(load_secrets().gcp_project)
    check = ga4.dry_run(client, cfg)
    typer.echo(check.describe())
    if dry_run:
        return
    if not check.within_cap:
        typer.echo("Refusing to run: over the configured cap.", err=True)
        raise typer.Exit(1)
    ga4.extract(client, cfg)
    typer.echo(f"Wrote {cfg.raw_path}\n")
    typer.echo(ga4.profile(ga4.load_raw(cfg.raw_path), cfg))


@app.command("simulate-spend")
def simulate_spend(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Generate simulated ad spend for paid channels (deterministic, labeled simulated)."""
    cfg = load_config(config)
    df = sim.simulate_spend(
        cfg.channels.paid, cfg.source.start_date, cfg.source.end_date, cfg.spend_simulation
    )
    sim.write_sim_spend(df, cfg.spend_simulation.output_path)
    typer.echo(f"Wrote {len(df)} rows to {cfg.spend_simulation.output_path} (SIMULATED spend)\n")
    df["week"] = [sim.iso_week_label(d) for d in df["date"].dt.date]
    weekly = (
        df.groupby(["channel", "week"], sort=True)
        .agg(spend=("spend", "sum"), planted=("planted_multiplier", "max"))
        .reset_index()
    )
    for row in weekly.itertuples(index=False):
        mark = f"  <- planted x{row.planted:g}" if row.planted != 1 else ""
        typer.echo(f"{row.channel}  {row.week}  {row.spend:>9,.2f}{mark}")


@app.command()
def validate(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Validate cached data; failing rows go to the quarantine folder with a reason."""
    cfg = load_config(config)
    src = cfg.source
    tables = {
        "ga4_daily": (
            ga4.load_raw(src.raw_path),
            schemas.ga4_daily_schema(src.start_date, src.end_date),
        ),
        "sim_spend": (
            pd.read_parquet(cfg.spend_simulation.output_path),
            schemas.sim_spend_schema(src.start_date, src.end_date, cfg.channels.paid),
        ),
    }
    for name, (df, schema) in tables.items():
        result = schemas.validate_and_quarantine(df, schema)
        path = schemas.write_quarantine(result, name, src.quarantine_dir)
        typer.echo(
            f"{name}: {len(result.valid)} valid, {result.quarantined_count} quarantined -> {path}"
        )
        for reason, count in result.quarantined["reason"].value_counts().items():
            typer.echo(f"  {count:>5}  {reason}")


@app.command("check-config")
def check_config(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Validate config.yaml and report which secrets are set (never their values)."""
    cfg = load_config(config)
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False).rstrip())
    secrets = load_secrets()
    typer.echo("\nsecrets:")
    for name, value in secrets.model_dump().items():
        typer.echo(f"  {name}: {'set' if value else 'missing'}")
