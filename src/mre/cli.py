"""Command-line entry point (``mre``)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from mre import __version__
from mre.config import DEFAULT_CONFIG_PATH, load_config, load_secrets

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


@app.command("check-config")
def check_config(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Validate config.yaml and report which secrets are set (never their values)."""
    cfg = load_config(config)
    typer.echo(yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False).rstrip())
    secrets = load_secrets()
    typer.echo("\nsecrets:")
    for name, value in secrets.model_dump().items():
        typer.echo(f"  {name}: {'set' if value else 'missing'}")
