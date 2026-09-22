"""Command-line entry point (``mre``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from mre import __version__
from mre.config import DEFAULT_CONFIG_PATH, load_config, load_secrets
from mre.sources import ga4_bigquery as ga4

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


@app.command("check-config")
def check_config(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Validate config.yaml and report which secrets are set (never their values)."""
    cfg = load_config(config)
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False).rstrip())
    secrets = load_secrets()
    typer.echo("\nsecrets:")
    for name, value in secrets.model_dump().items():
        typer.echo(f"  {name}: {'set' if value else 'missing'}")
