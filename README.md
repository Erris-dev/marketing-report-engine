# Marketing Report Engine

Turns the public GA4 e-commerce sample dataset into a weekly marketing analysis PDF:
extract (BigQuery, once) → validate → KPIs → anomalies → LLM narrative (guarded) → PDF → delivery.

> Work in progress.

## Development setup

```bash
uv sync
cp .env.example .env   # fill in secrets; never commit .env
uv run mre --help
uv run ruff check . && uv run mypy && uv run pytest
```
