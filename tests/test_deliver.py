import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest

from mre.config import DeliveryConfig, Secrets
from mre.deliver import DeliveryError, build_email, deliver
from mre.narrative import NarrativeOutput

NARRATIVE = NarrativeOutput(
    summary="Revenue rose. Sessions held. Spend is simulated.",
    findings=["One.", "Two.", "Three."],
    checks=["A.", "B.", "C."],
)
SECRETS = Secrets(
    smtp_user="me@gmail.com",
    smtp_password="app-password-123",  # type: ignore[arg-type]
    report_recipients=["a@example.com", "b@example.com"],
)


@dataclass
class FakeSMTP:
    """Records what a real smtplib.SMTP would have done; never opens a socket."""

    host: str
    port: int
    timeout: float = 0
    fail_on: str | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)
    sent: list[EmailMessage] = field(default_factory=list)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append(("quit", None))

    def starttls(self) -> None:
        self.calls.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        if self.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        self.calls.append(("login", user))

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "2020-W48.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    return path


def _factory(store: list[FakeSMTP], **kw: Any) -> Any:
    def make(host: str, port: int, timeout: float) -> FakeSMTP:
        smtp = FakeSMTP(host, port, timeout, **kw)
        store.append(smtp)
        return smtp

    return make


def test_email_is_sent_over_starttls_with_pdf_attached(pdf: Path) -> None:
    servers: list[FakeSMTP] = []
    cfg = DeliveryConfig(method="email")
    result = deliver(pdf, "2020-W48", NARRATIVE, "AI note.", cfg, SECRETS, _factory(servers))

    assert result.method == "email"
    assert result.detail == "emailed to 2 recipient(s)"
    smtp = servers[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert [c[0] for c in smtp.calls] == ["starttls", "login", "quit"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "Weekly marketing report 2020-W48"
    assert msg["To"] == "a@example.com, b@example.com"
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "2020-W48.pdf"
    assert attachment.get_content() == b"%PDF-1.7 fake"


def test_email_body_carries_disclosures(pdf: Path) -> None:
    msg = build_email(pdf, "2020-W48", NARRATIVE, "AI note.", "Report {week}", "me", ["a@x"])
    body = msg.get_body(preferencelist=("plain",))
    assert body is not None
    text = body.get_content()
    assert "Revenue rose." in text
    assert "AI note." in text
    assert "simulated" in text


def test_method_none_only_saves(pdf: Path) -> None:
    servers: list[FakeSMTP] = []
    result = deliver(
        pdf, "2020-W48", NARRATIVE, "", DeliveryConfig(method="none"), Secrets(), _factory(servers)
    )
    assert result.method == "none"
    assert servers == []


def test_missing_credentials_is_a_clear_error(pdf: Path) -> None:
    with pytest.raises(DeliveryError, match="SMTP_PASSWORD"):
        deliver(pdf, "2020-W48", NARRATIVE, "", DeliveryConfig(method="email"), Secrets())


def test_smtp_failure_is_wrapped_and_hides_password(pdf: Path) -> None:
    servers: list[FakeSMTP] = []
    with pytest.raises(DeliveryError, match="SMTPAuthenticationError") as err:
        deliver(
            pdf,
            "2020-W48",
            NARRATIVE,
            "",
            DeliveryConfig(method="email"),
            SECRETS,
            _factory(servers, fail_on="login"),
        )
    assert "app-password-123" not in str(err.value)
