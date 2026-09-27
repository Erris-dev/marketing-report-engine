"""Command-line entry point (``mre``)."""

from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from typing import Annotated

import pandas as pd
import typer
import yaml

from mre import __version__, schemas
from mre.config import DEFAULT_CONFIG_PATH, AppConfig, load_config, load_secrets
from mre.deliver import DeliveryError, deliver
from mre.facts import build_facts
from mre.metrics import complete_weeks
from mre.narrative import generate_narrative
from mre.pipeline import prepare
from mre.render import PdfUnavailableError, render_report
from mre.sources import ga4_bigquery as ga4
from mre.sources import sim_spend as sim
from mre.state import DEFAULT_STATE_PATH, RunState, load_state, next_week, save_state
from mre.weeks import iso_week_label

app = typer.Typer(
    help="Marketing Report Engine: GA4 e-commerce data to a weekly marketing PDF.",
    no_args_is_help=True,
)

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Path to config.yaml.", exists=True, dir_okay=False)
]
WeekOption = Annotated[str, typer.Option("--week", "-w", help="ISO week, e.g. 2020-W48.")]
FACTS_DIR = Path("out/facts")
REPORTS_DIR = Path("out/reports")


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
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("weasyprint", "fontTools", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # google-auth warns about user credentials without a quota project; harmless here.
    warnings.filterwarnings("ignore", message=".*quota project.*")


def _facts_for_week(cfg: AppConfig, week: str) -> dict[str, object]:
    prepared = prepare(cfg)
    weeks = complete_weeks(prepared.weekly)
    if week not in weeks:
        typer.echo(f"{week} is not a complete week in the data. Available: {', '.join(weeks)}")
        raise typer.Exit(1)
    facts = build_facts(week, prepared.weekly, prepared.anomalies, prepared.quarantined_rows, cfg)
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    (FACTS_DIR / f"{week}.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
    return facts


@app.command()
def facts(week: WeekOption, config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Build facts.json for a week (the only input the LLM sees)."""
    built = _facts_for_week(load_config(config), week)
    typer.echo(json.dumps(built, indent=2))
    typer.echo(f"\nWrote {FACTS_DIR / f'{week}.json'}", err=True)


@app.command()
def run(
    week: WeekOption,
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    out_dir: Annotated[Path, typer.Option(help="Where reports are written.")] = REPORTS_DIR,
    pdf: Annotated[bool, typer.Option("--pdf/--html-only", help="Build the PDF.")] = True,
    send: Annotated[
        bool,
        typer.Option("--deliver/--no-deliver", help="Deliver via delivery.method in config."),
    ] = True,
) -> None:
    """Build the weekly report for one ISO week and deliver it (email or none)."""
    cfg = load_config(config)
    secrets = load_secrets()
    key = secrets.openrouter_api_key
    try:
        paths = render_report(week, cfg, key.get_secret_value() if key else None, out_dir, pdf=pdf)
    except PdfUnavailableError as exc:
        typer.echo(f"{exc}\nThe HTML version is at {out_dir / f'{week}.html'}", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:  # unknown or partial week
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"narrative: {paths.narrative.source}")
    typer.echo(f"html: {paths.html}")
    if paths.pdf:
        typer.echo(f"pdf:  {paths.pdf}")
    if send and paths.pdf:
        try:
            result = deliver(
                paths.pdf,
                week,
                paths.narrative.output,
                paths.narrative.disclosure,
                cfg.delivery,
                secrets,
            )
        except DeliveryError as exc:
            typer.echo(f"delivery failed: {exc}", err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"delivery: {result.method} ({result.detail})")


@app.command()
def backfill(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    out_dir: Annotated[Path, typer.Option(help="Where reports are written.")] = REPORTS_DIR,
    pdf: Annotated[bool, typer.Option("--pdf/--html-only", help="Build PDFs.")] = True,
) -> None:
    """Build a report for every complete week in the data (no delivery)."""
    cfg = load_config(config)
    key = load_secrets().openrouter_api_key
    prepared = prepare(cfg)  # computed once, shared by every week
    weeks = complete_weeks(prepared.weekly)
    sources: dict[str, int] = {}
    for week in weeks:
        paths = render_report(
            week,
            cfg,
            key.get_secret_value() if key else None,
            out_dir,
            prepared=prepared,
            pdf=pdf,
        )
        sources[paths.narrative.source] = sources.get(paths.narrative.source, 0) + 1
        typer.echo(f"{week}: {paths.pdf or paths.html} ({paths.narrative.source})")
    summary = ", ".join(f"{n} {s}" for s, n in sorted(sources.items()))
    typer.echo(f"\n{len(weeks)} reports written to {out_dir} (narratives: {summary})")


@app.command()
def scheduled(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    state_path: Annotated[Path, typer.Option("--state", help="State file.")] = DEFAULT_STATE_PATH,
    out_dir: Annotated[Path, typer.Option(help="Where reports are written.")] = REPORTS_DIR,
    send: Annotated[
        bool,
        typer.Option("--deliver/--no-deliver", help="Deliver via delivery.method in config."),
    ] = True,
) -> None:
    """Report the next week in the dataset (see state.py) and advance the state file."""
    cfg = load_config(config)
    secrets = load_secrets()
    key = secrets.openrouter_api_key
    prepared = prepare(cfg)
    current = load_state(state_path)
    week = next_week(complete_weeks(prepared.weekly), current)
    typer.echo(f"scheduled run {current.runs + 1}: reporting {week}")
    paths = render_report(
        week, cfg, key.get_secret_value() if key else None, out_dir, prepared=prepared
    )
    typer.echo(f"pdf: {paths.pdf} (narrative: {paths.narrative.source})")
    if send and paths.pdf:
        try:
            result = deliver(
                paths.pdf,
                week,
                paths.narrative.output,
                paths.narrative.disclosure,
                cfg.delivery,
                secrets,
            )
        except DeliveryError as exc:
            typer.echo(f"delivery failed: {exc}", err=True)
            raise typer.Exit(2) from exc
        typer.echo(f"delivery: {result.method} ({result.detail})")
    # Advance only after the report (and delivery, if requested) succeeded.
    save_state(state_path, RunState(last_week=week, runs=current.runs + 1))


@app.command()
def serve(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    host: Annotated[str, typer.Option(help="Bind address (no auth: keep it local).")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Run the HTTP API (POST /reports, GET /reports/{week})."""
    import uvicorn

    from mre.api import create_app

    cfg = load_config(config)
    key = load_secrets().openrouter_api_key
    api = create_app(cfg, api_key=key.get_secret_value() if key else None, out_dir=REPORTS_DIR)
    uvicorn.run(api, host=host, port=port)


@app.command()
def narrate(week: WeekOption, config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Generate the guarded AI narrative for a week (falls back to the template)."""
    cfg = load_config(config)
    built = _facts_for_week(cfg, week)
    key = load_secrets().openrouter_api_key
    result = generate_narrative(built, cfg.llm, key.get_secret_value() if key else None)
    reason = f" ({result.fallback_reason})" if result.fallback_reason else ""
    typer.echo(f"source: {result.source}{reason}")
    if result.unmatched_numbers:
        typer.echo(f"numbers rejected by the guard: {', '.join(result.unmatched_numbers)}")
    typer.echo(f"\n{result.output.summary}\n\nFindings:")
    for item in result.output.findings:
        typer.echo(f"  - {item}")
    typer.echo("\nThings to check:")
    for item in result.output.checks:
        typer.echo(f"  - {item}")
    typer.echo(f"\n{result.disclosure}")


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
    df["week"] = [iso_week_label(d) for d in df["date"].dt.date]
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
            ga4.load_raw(src.input_path()),
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
