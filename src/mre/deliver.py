"""Report delivery: Gmail over SMTP (STARTTLS), or ``none`` to only keep the file.

Credentials come from ``.env`` (a Gmail app password, never the account password).
Missing credentials are a clear error rather than a silent skip, and recipient
addresses are never written to logs.
"""

from __future__ import annotations

import logging
import smtplib
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from mre.config import DeliveryConfig, Secrets
from mre.narrative import NarrativeOutput

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 30
SIMULATED_NOTE = "Ad spend and spend-based metrics in this report are simulated."


class DeliveryError(RuntimeError):
    """Delivery was requested but could not be completed."""


@dataclass(frozen=True)
class DeliveryResult:
    method: str
    detail: str


def build_email(
    pdf: Path,
    week: str,
    narrative: NarrativeOutput,
    disclosure: str,
    subject_template: str,
    sender: str,
    recipients: list[str],
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject_template.format(week=week)
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    findings = "\n".join(f"- {f}" for f in narrative.findings)
    msg.set_content(
        f"Weekly marketing report for {week} (PDF attached).\n\n"
        f"{narrative.summary}\n\nKey findings:\n{findings}\n\n"
        f"{disclosure}\n{SIMULATED_NOTE}\n"
    )
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    return msg


def send_email(
    msg: EmailMessage,
    cfg: DeliveryConfig,
    secrets: Secrets,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> None:
    if not (secrets.smtp_user and secrets.smtp_password and secrets.report_recipients):
        raise DeliveryError(
            "Email delivery needs SMTP_USER, SMTP_PASSWORD and REPORT_RECIPIENTS in .env "
            "(use a Gmail app password). Set delivery.method to 'none' to skip delivery."
        )
    email = cfg.email
    try:
        with smtp_factory(email.smtp_host, email.smtp_port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            if email.use_starttls:
                smtp.starttls()
            smtp.login(secrets.smtp_user, secrets.smtp_password.get_secret_value())
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        # The exception text can echo the server reply but never the password.
        raise DeliveryError(f"Sending email failed: {type(exc).__name__}: {exc}") from exc


def deliver(
    pdf: Path,
    week: str,
    narrative: NarrativeOutput,
    disclosure: str,
    cfg: DeliveryConfig,
    secrets: Secrets,
    smtp_factory: Callable[..., Any] = smtplib.SMTP,
) -> DeliveryResult:
    if cfg.method == "none":
        return DeliveryResult("none", f"saved to {pdf}")
    sender = secrets.smtp_user or ""
    msg = build_email(
        pdf,
        week,
        narrative,
        disclosure,
        cfg.email.subject_template,
        sender,
        secrets.report_recipients,
    )
    send_email(msg, cfg, secrets, smtp_factory)
    count = len(secrets.report_recipients)
    log.info("Emailed %s to %d recipient(s)", pdf.name, count)
    return DeliveryResult("email", f"emailed to {count} recipient(s)")
