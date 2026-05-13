"""
alerter.py — Alert dispatch (§9.3).

PURPOSE:
    Render alert templates and send them through both Email and SMS
    channels (D-AT-1: every alert goes to both channels, no exceptions).

DESIGN:
    - `Alerter` is a Protocol implemented by two concrete classes:
      * `StdoutAlerter`: prints alerts to stdout. Used by IPMS (the
        simulator at c:\\portfolio\\IPMS\\) and by ad-hoc dev runs.
      * `GmailTwilioAlerter`: sends Email via SMTP (smtp.gmail.com)
        and SMS via the Twilio REST API. Production deployment uses
        this.
    - Templates load from alert_templates.yaml at construction; missing
      template keys are a startup failure (I9: invalid config aborts).
    - Dedup: per §11.2.10, a (alert_id, dedup_key) pair fires at most
      once per dedup_window (default 24 hours), preventing alert
      storms. The dedup_key derives from the alert's context dict
      unless the emitter sets it explicitly on the AlertEntry.
    - Failures of one channel do not block the other (§11.2.11): we
      attempt both, log failures, and the next cycle's
      `alerter_failure_recovered` Warning fires when a previously-
      failed channel comes back.
"""

from __future__ import annotations

import hashlib
import logging
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from alert_catalog import (
    CRITICAL_CATEGORY,
    DEFAULT_SEVERITY,
    AlertId,
    CriticalCategory,
    Severity,
)
from plan_model import AlertEntry

logger = logging.getLogger(__name__)


# =============================================================================
# Rendered alert (internal helper)
# =============================================================================

@dataclass(frozen=True)
class RenderedAlert:
    """An alert after template substitution, ready to dispatch."""
    alert_id: str
    severity: Severity
    critical_category: CriticalCategory | None
    subject: str
    body: str
    sms: str
    timestamp: datetime
    dedup_key: str


# =============================================================================
# Template loader
# =============================================================================

@dataclass(frozen=True)
class AlertTemplate:
    subject: str
    body: str
    sms: str


def load_templates(yaml_path: str | Path) -> dict[str, AlertTemplate]:
    """Load alert_templates.yaml into a dict keyed by alert_id.

    Raises ValueError if any AlertId is missing a template or if any
    template is missing required keys.
    """
    p = Path(yaml_path)
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: root must be a mapping")
    templates: dict[str, AlertTemplate] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue  # ignore non-mapping entries (e.g. YAML anchors)
        for required in ("subject", "body", "sms"):
            if required not in v:
                raise ValueError(
                    f"{p}: alert {k!r} missing required key {required!r}"
                )
        templates[k] = AlertTemplate(
            subject=str(v["subject"]),
            body=str(v["body"]),
            sms=str(v["sms"]),
        )
    # Verify coverage of AlertId
    missing = [a.value for a in AlertId if a.value not in templates]
    if missing:
        raise ValueError(
            f"{p}: alert_templates.yaml missing entries for {missing}"
        )
    return templates


# =============================================================================
# Template rendering
# =============================================================================

def _safe_format(template: str, context: dict[str, str]) -> str:
    """Substitute {placeholders} from `context`; unknown placeholders
    fall through as literal text (per alert_templates.yaml header).

    We avoid str.format because a single unknown placeholder raises
    KeyError; we want graceful degradation.
    """
    out = template
    for key, val in context.items():
        out = out.replace("{" + key + "}", val)
    return out


def _derive_dedup_key(alert_id: str, context: dict[str, str]) -> str:
    """Default dedup key: stable hash of (alert_id, sorted-context).

    Emitters can override via AlertEntry.dedup_key.
    """
    items = sorted(context.items())
    payload = alert_id + "|" + "|".join(f"{k}={v}" for k, v in items)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def render_alert(
    entry: AlertEntry,
    templates: dict[str, AlertTemplate],
    *,
    now: datetime,
    default_context: dict[str, str] | None = None,
) -> RenderedAlert:
    """Apply templates and context to produce a RenderedAlert.

    `default_context` provides values for the common placeholders
    documented in alert_templates.yaml (timestamp, cycle_id, box_id,
    phase, cb_state). The entry's own context overrides defaults.
    """
    tmpl = templates.get(entry.alert_id)
    if tmpl is None:
        # Unknown alert_id: should be impossible since load_templates
        # checks coverage of AlertId, but defend against direct string
        # use by callers.
        raise KeyError(f"no template for alert_id {entry.alert_id!r}")

    ctx: dict[str, str] = {}
    if default_context:
        ctx.update(default_context)
    ctx.update(entry.context)
    ctx.setdefault("timestamp", now.isoformat())

    # Determine severity (override > catalog default)
    severity: Severity
    if entry.severity_override:
        try:
            severity = Severity(entry.severity_override)
        except ValueError:
            severity = Severity.WARNING
            logger.warning(
                "alert %s: unknown severity_override %r; using WARNING",
                entry.alert_id, entry.severity_override,
            )
    else:
        try:
            aid = AlertId(entry.alert_id)
            severity = DEFAULT_SEVERITY.get(aid, Severity.NOTICE)
        except ValueError:
            severity = Severity.NOTICE

    try:
        aid_enum = AlertId(entry.alert_id)
        critical_cat = CRITICAL_CATEGORY.get(aid_enum)
    except ValueError:
        critical_cat = None

    subject = _safe_format(tmpl.subject, ctx)
    body = _safe_format(tmpl.body, ctx)
    sms = _safe_format(tmpl.sms, ctx)

    dedup_key = entry.dedup_key or _derive_dedup_key(entry.alert_id, ctx)
    return RenderedAlert(
        alert_id=entry.alert_id,
        severity=severity,
        critical_category=critical_cat,
        subject=subject,
        body=body,
        sms=sms,
        timestamp=now,
        dedup_key=dedup_key,
    )


# =============================================================================
# Dedup window
# =============================================================================

class DedupTracker:
    """In-process dedup tracker. Keys live for `window` seconds.

    Per §11.2.10 the dedup state is per-run and not persisted: a
    process restart re-emits any "duplicate" alerts. That is the safe
    behavior — a restart implies operator attention is needed anyway.
    """

    def __init__(self, window: timedelta = timedelta(hours=24)) -> None:
        self._window = window
        self._seen: dict[tuple[str, str], datetime] = {}

    def should_send(self, alert_id: str, dedup_key: str,
                    now: datetime) -> bool:
        """True iff (alert_id, dedup_key) was not seen within `window`."""
        # Garbage-collect expired keys lazily
        cutoff = now - self._window
        for k, ts in list(self._seen.items()):
            if ts < cutoff:
                del self._seen[k]
        key = (alert_id, dedup_key)
        if key in self._seen:
            return False
        self._seen[key] = now
        return True


# =============================================================================
# Alerter Protocol
# =============================================================================

@runtime_checkable
class Alerter(Protocol):
    """Implementations dispatch RenderedAlerts to their configured channels."""

    def dispatch(self, entry: AlertEntry,
                 *, default_context: dict[str, str] | None = None,
                 now: datetime | None = None) -> "DispatchOutcome":
        ...


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of one dispatch attempt across both channels."""
    rendered: RenderedAlert
    email_ok: bool
    sms_ok: bool
    email_error: str | None = None
    sms_error: str | None = None
    deduped: bool = False  # True if not sent due to dedup


# =============================================================================
# StdoutAlerter (sim/dev)
# =============================================================================

class StdoutAlerter:
    """Prints alerts to stdout in a readable format.

    Used by the IPMS simulator (c:\\portfolio\\IPMS\\) and by dev runs.
    Both "channels" succeed trivially; we just print twice with
    differentiating headers so the output mirrors production
    semantics.
    """

    def __init__(self,
                 templates: dict[str, AlertTemplate],
                 *,
                 dedup: DedupTracker | None = None) -> None:
        self._templates = templates
        self._dedup = dedup or DedupTracker()

    def dispatch(self, entry: AlertEntry,
                 *, default_context: dict[str, str] | None = None,
                 now: datetime | None = None) -> DispatchOutcome:
        ts = now or datetime.now(timezone.utc)
        rendered = render_alert(
            entry, self._templates,
            now=ts, default_context=default_context,
        )
        if not self._dedup.should_send(
            rendered.alert_id, rendered.dedup_key, ts
        ):
            return DispatchOutcome(
                rendered=rendered, email_ok=True, sms_ok=True, deduped=True,
            )

        # Email-style block
        print("=" * 70)
        print(f"[EMAIL] [{rendered.severity.value}] {rendered.subject}")
        print(f"        alert_id={rendered.alert_id}  "
              f"dedup_key={rendered.dedup_key}")
        print("-" * 70)
        print(rendered.body)
        print("=" * 70)
        # SMS line
        print(f"[SMS]   [{rendered.severity.value}] {rendered.sms}")
        print()
        return DispatchOutcome(
            rendered=rendered, email_ok=True, sms_ok=True,
        )


# =============================================================================
# GmailTwilioAlerter (production)
# =============================================================================

@dataclass(frozen=True)
class GmailConfig:
    """SMTP credentials for the Gmail account that dispatches alerts.

    The operator obtains an App Password from Google Account settings
    (regular Gmail passwords don't work for SMTP after 2022). Stored
    in box.yaml or .env per §15.8.
    """
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465  # SSL
    username: str = ""       # full Gmail address
    app_password: str = ""   # 16-char app password
    from_address: str = ""   # typically == username
    to_addresses: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwilioConfig:
    """Twilio account credentials for SMS dispatch.

    Obtained from twilio.com/console. The from_number must be a
    Twilio-purchased phone number. Stored in box.yaml or .env.
    """
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""    # E.164, e.g. +14155550100
    to_numbers: tuple[str, ...] = ()


class GmailTwilioAlerter:
    """Production alerter: Gmail SMTP + Twilio REST API.

    Per §11.2.11, transient SMTP or Twilio failures don't block
    other-channel delivery. The DispatchOutcome reports per-channel
    success; the caller (decision/action layer) decides whether to
    re-fire on the next cycle.
    """

    def __init__(self,
                 templates: dict[str, AlertTemplate],
                 gmail: GmailConfig,
                 twilio: TwilioConfig,
                 *,
                 dedup: DedupTracker | None = None,
                 timeout_seconds: int = 15) -> None:
        self._templates = templates
        self._gmail = gmail
        self._twilio = twilio
        self._dedup = dedup or DedupTracker()
        self._timeout = timeout_seconds

    # --- Email ---
    def _send_email(self, rendered: RenderedAlert) -> tuple[bool, str | None]:
        if not (self._gmail.username and self._gmail.app_password
                and self._gmail.from_address and self._gmail.to_addresses):
            return False, "GmailConfig incomplete; check box.yaml/.env"

        msg = EmailMessage()
        # Severity tag in subject for at-a-glance triage
        msg["Subject"] = f"[IRAPM {rendered.severity.value}] {rendered.subject}"
        msg["From"] = self._gmail.from_address
        msg["To"] = ", ".join(self._gmail.to_addresses)
        msg.set_content(rendered.body)
        try:
            with smtplib.SMTP_SSL(self._gmail.smtp_host,
                                  self._gmail.smtp_port,
                                  timeout=self._timeout) as srv:
                srv.login(self._gmail.username, self._gmail.app_password)
                srv.send_message(msg)
            return True, None
        except (smtplib.SMTPException, OSError) as e:
            return False, f"{type(e).__name__}: {e}"

    # --- SMS via Twilio REST ---
    def _send_sms(self, rendered: RenderedAlert) -> tuple[bool, str | None]:
        if not (self._twilio.account_sid and self._twilio.auth_token
                and self._twilio.from_number and self._twilio.to_numbers):
            return False, "TwilioConfig incomplete; check box.yaml/.env"

        # Import lazily so dev boxes without `requests` installed can
        # still exercise StdoutAlerter and the rest of the system.
        try:
            import requests  # type: ignore
        except ImportError:
            return False, "python-requests not installed on deploy box"

        url = (f"https://api.twilio.com/2010-04-01/Accounts/"
               f"{self._twilio.account_sid}/Messages.json")
        auth = (self._twilio.account_sid, self._twilio.auth_token)
        # Prefix severity tag for at-a-glance SMS triage
        body = f"[IRAPM {rendered.severity.value}] {rendered.sms}"
        ok_any = False
        last_err: str | None = None
        for to in self._twilio.to_numbers:
            data = {
                "From": self._twilio.from_number,
                "To": to,
                "Body": body,
            }
            try:
                r = requests.post(url, auth=auth, data=data,
                                  timeout=self._timeout)
                if 200 <= r.status_code < 300:
                    ok_any = True
                else:
                    last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
        if ok_any and last_err is None:
            return True, None
        if ok_any:
            return True, f"partial: {last_err}"
        return False, last_err or "no recipients succeeded"

    def dispatch(self, entry: AlertEntry,
                 *, default_context: dict[str, str] | None = None,
                 now: datetime | None = None) -> DispatchOutcome:
        ts = now or datetime.now(timezone.utc)
        rendered = render_alert(
            entry, self._templates,
            now=ts, default_context=default_context,
        )
        if not self._dedup.should_send(
            rendered.alert_id, rendered.dedup_key, ts
        ):
            return DispatchOutcome(
                rendered=rendered, email_ok=True, sms_ok=True, deduped=True,
            )

        email_ok, email_err = self._send_email(rendered)
        sms_ok, sms_err = self._send_sms(rendered)
        if not email_ok:
            logger.warning("email channel failed for %s: %s",
                           rendered.alert_id, email_err)
        if not sms_ok:
            logger.warning("sms channel failed for %s: %s",
                           rendered.alert_id, sms_err)
        return DispatchOutcome(
            rendered=rendered,
            email_ok=email_ok,
            sms_ok=sms_ok,
            email_error=email_err,
            sms_error=sms_err,
        )


__all__ = [
    "AlertTemplate",
    "RenderedAlert",
    "DedupTracker",
    "Alerter",
    "DispatchOutcome",
    "StdoutAlerter",
    "GmailConfig",
    "TwilioConfig",
    "GmailTwilioAlerter",
    "load_templates",
    "render_alert",
]
