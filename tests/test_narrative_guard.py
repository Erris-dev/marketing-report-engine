import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from mre.config import AppConfig
from mre.narrative import (
    AI_DISCLOSURE,
    TEMPLATE_DISCLOSURE,
    NarrativeOutput,
    check_numbers,
    count_sentences,
    extract_numbers,
    generate_narrative,
    parse_response,
    template_narrative,
)

GUARD_FACTS: dict[str, Any] = {
    "report_week": "2020-W48",
    "totals": {"sessions": 27780, "revenue": 40146.0, "wow_revenue_pct": 51.2},
    "channels": [{"roas": 0.73, "wow_sessions_pct": -12.3, "revenue": 1_234_567.0}],
    "flags": {"simulated": True},
}


def _output(summary_extra: str = "", finding: str = "Organic search grew.") -> NarrativeOutput:
    return NarrativeOutput(
        summary=f"Traffic was steady. Revenue rose. Paid spend is simulated.{summary_extra}",
        findings=[finding, "Referral held its share.", "Direct was flat."],
        checks=["Check tagging.", "Check checkout.", "Check the unknown channel."],
    )


def _guard(text: str) -> list[str]:
    return check_numbers(_output(finding=text), GUARD_FACTS).unmatched


# --- number guard ------------------------------------------------------------------


def test_guard_accepts_exact_numbers() -> None:
    text = "Revenue was $40,146.00 (up 51.2%) from 27,780 sessions; ROAS was 0.73 (simulated)."
    assert _guard(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Revenue rose about 51%.",  # rounded to fewer decimals
        "Revenue reached $40.1k.",  # k suffix, 1 decimal
        "Revenue reached $40k.",
        "Revenue was 40,146 dollars.",
        "Sessions reached 27.8k.",
        "Revenue exceeded $1.2m in that channel.",  # m suffix
        "Sessions fell 12.3% week over week.",  # sign dropped for a negative fact
    ],
)
def test_guard_tolerates_rounding_and_formatting(text: str) -> None:
    assert _guard(text) == []


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Revenue grew 60% this week.", "60"),
        ("Revenue grew 52%.", "52"),  # 51.2 does not round to 52
        ("Revenue was $41,000.", "41,000"),
        ("Sessions doubled to 55,560.", "55,560"),  # a computed number
        ("Revenue grew 51.3%.", "51.3"),
    ],
)
def test_guard_rejects_invented_numbers(text: str, bad: str) -> None:
    assert _guard(text) == [bad]


def test_guard_allows_years_week_labels_and_dates() -> None:
    assert _guard("In 2020-W48 (starting 2020-11-23) and into 2021, revenue was $40k.") == []


def test_guard_rejects_non_year_decimal_like_a_year() -> None:
    assert _guard("A score of 2021.5 was reached.") == ["2021.5"]


def test_extract_numbers_parses_formats() -> None:
    tokens = {t.text: (t.value, t.tolerance) for t in extract_numbers("$1,234.5 and 3k and 12%")}
    assert tokens["1,234.5"] == (1234.5, 0.05)
    assert tokens["3k"] == (3000.0, 500.0)
    assert tokens["12"] == (12.0, 0.5)


def test_guard_checks_summary_findings_and_checks() -> None:
    out = _output(summary_extra=" Revenue hit 99.")
    assert check_numbers(out, GUARD_FACTS).unmatched == ["99"]


# --- output schema -----------------------------------------------------------------


def test_sentence_counting_ignores_decimals() -> None:
    assert count_sentences("Revenue was $1.2k. ROAS was 0.73. It is simulated.") == 3


@pytest.mark.parametrize(
    ("summary", "findings"),
    [
        ("One sentence only.", ["a", "b", "c"]),
        ("A. B. C. D. E. F.", ["a", "b", "c"]),
        ("A one. B two. C three.", ["a", "b"]),
        ("A one. B two. C three.", ["a", "b", " "]),
    ],
)
def test_schema_rejects_bad_shapes(summary: str, findings: list[str]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError subclasses it
        NarrativeOutput(summary=summary, findings=findings, checks=["x", "y", "z"])


def test_parse_response_tolerates_code_fences() -> None:
    payload = {
        "summary": "One. Two. Three.",
        "findings": ["a", "b", "c"],
        "checks": ["d", "e", "f"],
    }
    assert parse_response(f"```json\n{json.dumps(payload)}\n```").findings == ["a", "b", "c"]


# --- template fallback ---------------------------------------------------------------


def test_template_passes_its_own_guard(facts: dict[str, Any]) -> None:
    out = template_narrative(facts)
    assert check_numbers(out, facts).ok
    assert "simulated" in out.summary


def test_template_without_previous_week(prepared: Any, config: AppConfig) -> None:
    from mre.facts import build_facts

    facts = build_facts("2020-W52", prepared.weekly, prepared.anomalies, 0, config)
    out = template_narrative(facts)
    assert "no previous complete week" in out.summary
    assert check_numbers(out, facts).ok


# --- LLM flow (mocked client) ----------------------------------------------------------


@dataclass
class FakeClient:
    """Mimics ``client.chat.completions.create``; replies are consumed in order."""

    replies: list[str | Exception]
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, *, model: str, temperature: float, messages: list[dict[str, str]]) -> Any:
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200),
        )


def _good_reply(facts: dict[str, Any]) -> str:
    t = facts["totals"]
    return json.dumps(
        {
            "summary": (
                f"In {facts['report_week']} revenue was ${t['revenue']:,.2f}. "
                f"Revenue rose {t['wow_revenue_pct']}% compared with {facts['previous_week']}. "
                "Ad spend figures are simulated."
            ),
            "findings": ["Direct revenue rose.", "Paid search is simulated.", "Unknown held."],
            "checks": ["Check tagging.", "Check checkout.", "Check attribution."],
        }
    )


def test_valid_reply_is_used_and_cached(facts: dict[str, Any], config: AppConfig) -> None:
    client = FakeClient([_good_reply(facts)])
    result = generate_narrative(facts, config.llm, api_key=None, client=client)
    assert result.source == "llm"
    assert result.disclosure == AI_DISCLOSURE
    assert (result.input_tokens, result.output_tokens) == (1000, 200)
    assert result.cost_usd == pytest.approx((1000 * 0.25 + 200 * 1.50) / 1_000_000)

    # Same facts again: served from cache, the client is not called.
    empty = FakeClient([])
    cached = generate_narrative(facts, config.llm, api_key=None, client=empty)
    assert cached.source == "cache"
    assert cached.output == result.output
    assert empty.calls == []


def test_llm_only_receives_facts(facts: dict[str, Any], config: AppConfig) -> None:
    client = FakeClient([_good_reply(facts)])
    generate_narrative(facts, config.llm, api_key=None, client=client)
    system, user = client.calls[0]
    assert system["role"] == "system"
    assert "Use only numbers that appear in the facts" in system["content"]
    assert json.loads(user["content"].removeprefix("facts = ")) == facts


def test_invalid_json_is_retried_once_with_the_error(
    facts: dict[str, Any], config: AppConfig
) -> None:
    client = FakeClient(["not json at all", _good_reply(facts)])
    result = generate_narrative(facts, config.llm, api_key=None, client=client)
    assert result.source == "llm"
    assert len(client.calls) == 2
    assert "Your reply was invalid" in client.calls[1][-1]["content"]
    assert result.input_tokens == 2000  # usage summed across attempts


def test_invalid_json_twice_falls_back(facts: dict[str, Any], config: AppConfig) -> None:
    wrong_shape = json.dumps({"summary": "Too short.", "findings": [], "checks": []})
    result = generate_narrative(
        facts, config.llm, api_key=None, client=FakeClient(["{oops", wrong_shape])
    )
    assert result.source == "fallback"
    assert result.disclosure == TEMPLATE_DISCLOSURE
    assert "invalid JSON" in (result.fallback_reason or "")
    assert check_numbers(result.output, facts).ok


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), RuntimeError("HTTP 503")])
def test_api_failure_falls_back(facts: dict[str, Any], config: AppConfig, error: Exception) -> None:
    result = generate_narrative(facts, config.llm, api_key=None, client=FakeClient([error]))
    assert result.source == "fallback"
    assert "API call failed" in (result.fallback_reason or "")


def test_guard_failure_falls_back_and_is_not_cached(
    facts: dict[str, Any], config: AppConfig
) -> None:
    reply = json.loads(_good_reply(facts))
    reply["findings"][0] = "Direct revenue grew 987.6% thanks to the new campaign."
    client = FakeClient([json.dumps(reply)])
    result = generate_narrative(facts, config.llm, api_key=None, client=client)
    assert result.source == "fallback"
    assert result.fallback_reason == "number guard failed"
    assert result.unmatched_numbers == ["987.6"]
    assert not any(config.llm.cache_dir.glob("*.json"))


def test_missing_api_key_falls_back_without_network(
    facts: dict[str, Any], config: AppConfig
) -> None:
    result = generate_narrative(facts, config.llm, api_key=None)
    assert result.source == "fallback"
    assert "OPENROUTER_API_KEY" in (result.fallback_reason or "")


def test_api_key_is_never_logged(
    facts: dict[str, Any], config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sk-or-v1-super-secret"
    caplog.set_level(logging.DEBUG)
    generate_narrative(facts, config.llm, api_key=secret, client=FakeClient([RuntimeError("boom")]))
    assert secret not in caplog.text
