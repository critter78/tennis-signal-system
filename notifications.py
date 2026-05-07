#!/usr/bin/env python3
"""
notifications.py — drop-in multi-channel alert library

A self-contained module you can copy into any Python project.
Four independent channel adapters with a common interface:

    TelegramChannel   — Telegram Bot API (free, instant, low-noise bot)
    WhatsAppChannel   — Twilio WhatsApp Business API (free sandbox, ~$0.005/msg prod)
    EmailChannel      — SMTP (works with Gmail, Resend, SES, etc.)
    SMSChannel        — Twilio SMS REST API (paid, ~$0.008-0.09/SMS by country)

Each channel auto-enables when its env vars are set; otherwise silently
skipped. No code changes needed to add or remove channels per deployment.

Quick start:
    from notifications import Notifier
    notifier = Notifier.from_env()
    notifier.send("Test", "Hello world", level="info")

The Notifier fans out to all configured channels. To target a subset:
    notifier.send("Subject", "Body", channels=["telegram", "whatsapp"])

Routing helper for tiered alerts (used by NBA Comeback Radar):
    notifier.send_tiered(subject, body, conviction=2)
    # default: 1=tg, 2=tg+whatsapp+email, 3=all four
"""

import json
import logging
import os
import smtplib
import threading
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


# ─── Common channel interface ────────────────────────────────────────────────

class NotificationChannel(ABC):
    """Subclass + implement send() and is_configured()."""
    name: str = "channel"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def send(self, subject: str, body: str,
             level: str = "info",
             metadata: Optional[dict] = None) -> dict: ...


# ─── Telegram ────────────────────────────────────────────────────────────────

class TelegramChannel(NotificationChannel):
    """Telegram Bot API. Free, instant, supports markdown.

    Setup:
      1. Create bot via @BotFather → save bot token
      2. Message your bot once
      3. GET https://api.telegram.org/bot<TOKEN>/getUpdates → find chat.id
      4. Set env vars TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
    """
    name = "telegram"

    def __init__(self, bot_token: Optional[str] = None,
                 chat_id: Optional[str] = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, subject: str, body: str,
             level: str = "info",
             metadata: Optional[dict] = None) -> dict:
        if not self.is_configured():
            return {"ok": False, "channel": self.name, "error": "not_configured"}

        # Markdown-V1 supports *bold*, _italic_, `code` and [link](url)
        text = f"*{subject}*\n\n{body}"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={
                    "chat_id":                  self.chat_id,
                    "text":                     text,
                    "parse_mode":               "Markdown",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            r.raise_for_status()
            return {"ok": True, "channel": self.name,
                    "message_id": r.json().get("result", {}).get("message_id")}
        except requests.HTTPError as e:
            return {"ok": False, "channel": self.name,
                    "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            return {"ok": False, "channel": self.name, "error": str(e)}


# ─── Email via SMTP ──────────────────────────────────────────────────────────

class EmailChannel(NotificationChannel):
    """SMTP — works with any provider that supports SMTP (Gmail, Resend, SES…).

    Setup for Gmail:
      1. Enable 2FA on your Google account
      2. Create app password: https://myaccount.google.com/apppasswords
      3. Set env vars:
           SMTP_HOST=smtp.gmail.com
           SMTP_PORT=587
           SMTP_USER=you@gmail.com
           SMTP_PASS=<app password>
           EMAIL_FROM=you@gmail.com
           EMAIL_TO=alerts@yourdomain.com,partner@example.com   (comma-separated)
    """
    name = "email"

    def __init__(self,
                 smtp_host:  Optional[str] = None,
                 smtp_port:  Optional[int] = None,
                 smtp_user:  Optional[str] = None,
                 smtp_pass:  Optional[str] = None,
                 from_addr:  Optional[str] = None,
                 to_addrs:   Optional[list[str]] = None,
                 use_tls:    bool = True):
        self.smtp_host = smtp_host or os.environ.get("SMTP_HOST")
        self.smtp_port = int(smtp_port or os.environ.get("SMTP_PORT", "587") or 587)
        self.smtp_user = smtp_user or os.environ.get("SMTP_USER")
        self.smtp_pass = smtp_pass or os.environ.get("SMTP_PASS")
        self.from_addr = from_addr or os.environ.get("EMAIL_FROM") or self.smtp_user
        if to_addrs is None:
            raw = os.environ.get("EMAIL_TO", "")
            to_addrs = [a.strip() for a in raw.split(",") if a.strip()]
        self.to_addrs = to_addrs
        self.use_tls = use_tls

    def is_configured(self) -> bool:
        return all([self.smtp_host, self.smtp_user, self.smtp_pass,
                    self.from_addr, self.to_addrs])

    def send(self, subject: str, body: str,
             level: str = "info",
             metadata: Optional[dict] = None) -> dict:
        if not self.is_configured():
            return {"ok": False, "channel": self.name, "error": "not_configured"}

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)

        # Plain-text body. Easy upgrade later: HTML alternative.
        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as srv:
                if self.use_tls:
                    srv.starttls()
                srv.login(self.smtp_user, self.smtp_pass)
                srv.sendmail(self.from_addr, self.to_addrs, msg.as_string())
            return {"ok": True, "channel": self.name,
                    "recipients": len(self.to_addrs)}
        except Exception as e:
            return {"ok": False, "channel": self.name, "error": str(e)}


# ─── SMS via Twilio ──────────────────────────────────────────────────────────

class SMSChannel(NotificationChannel):
    """Twilio REST API — no SDK dependency, just requests.

    Setup:
      1. twilio.com sign up + verify phone
      2. Buy a phone number ($1.15/mo US)
      3. Set env vars:
           TWILIO_ACCOUNT_SID=ACxxxxxxx
           TWILIO_AUTH_TOKEN=xxxxxx
           TWILIO_FROM_NUMBER=+15551234567
           TWILIO_TO_NUMBERS=+15555551234,+15555556789   (comma-separated)
    """
    name = "sms"

    def __init__(self,
                 twilio_sid:    Optional[str] = None,
                 twilio_token:  Optional[str] = None,
                 from_number:   Optional[str] = None,
                 to_numbers:    Optional[list[str]] = None):
        self.twilio_sid   = twilio_sid   or os.environ.get("TWILIO_ACCOUNT_SID")
        self.twilio_token = twilio_token or os.environ.get("TWILIO_AUTH_TOKEN")
        self.from_number  = from_number  or os.environ.get("TWILIO_FROM_NUMBER")
        if to_numbers is None:
            raw = os.environ.get("TWILIO_TO_NUMBERS", "")
            to_numbers = [n.strip() for n in raw.split(",") if n.strip()]
        self.to_numbers = to_numbers

    def is_configured(self) -> bool:
        return all([self.twilio_sid, self.twilio_token,
                    self.from_number, self.to_numbers])

    def send(self, subject: str, body: str,
             level: str = "info",
             metadata: Optional[dict] = None) -> dict:
        if not self.is_configured():
            return {"ok": False, "channel": self.name, "error": "not_configured"}

        # SMS is plain text; collapse subject + body and trim to 1600 chars
        # (Twilio limit; longer is split into multiple billable segments).
        full = f"{subject}\n\n{body}"
        if len(full) > 1500:
            full = full[:1497] + "..."

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_sid}/Messages.json"
        results = []
        for to in self.to_numbers:
            try:
                r = requests.post(
                    url,
                    auth=(self.twilio_sid, self.twilio_token),
                    data={"From": self.from_number, "To": to, "Body": full},
                    timeout=15,
                )
                r.raise_for_status()
                results.append({"to": to, "ok": True,
                                "sid": r.json().get("sid")})
            except requests.HTTPError as e:
                results.append({"to": to, "ok": False,
                                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"})
            except Exception as e:
                results.append({"to": to, "ok": False, "error": str(e)})

        ok = all(r["ok"] for r in results)
        return {"ok": ok, "channel": self.name, "results": results}


# ─── WhatsApp via Twilio ─────────────────────────────────────────────────────

class WhatsAppChannel(NotificationChannel):
    """Twilio WhatsApp Business API — same Messages endpoint as SMS, with a
    `whatsapp:` URI prefix on From/To numbers.

    Why this is great: free sandbox, ~10x cheaper than international SMS, native
    formatting (*bold*, _italic_, ~strike~), instant push on personal phones.

    Setup (free sandbox — fastest path, fine for personal alerts forever):
      1. Twilio Console → Messaging → Try it out → Send a WhatsApp message
      2. Send the join code (e.g. "join lone-ground") from your WhatsApp to
         the sandbox number Twilio shows you (typically +1 415 523 8886)
      3. Set env vars:
           TWILIO_ACCOUNT_SID=ACxxxxxxx
           TWILIO_AUTH_TOKEN=xxxxxx
           TWILIO_WHATSAPP_FROM=+14155238886            (sandbox; omit to default)
           TWILIO_WHATSAPP_TO=+15551234567,+15555556789  (comma-separated)

    Setup (production — required to send to anyone, no 24h window, custom From):
      Twilio Console → Messaging → Senders → WhatsApp Senders → Create new sender.
      Requires Facebook Business Manager + a phone number you own.
    """
    name = "whatsapp"
    DEFAULT_SANDBOX_FROM = "+14155238886"

    def __init__(self,
                 twilio_sid:    Optional[str] = None,
                 twilio_token:  Optional[str] = None,
                 from_number:   Optional[str] = None,
                 to_numbers:    Optional[list[str]] = None):
        self.twilio_sid   = twilio_sid   or os.environ.get("TWILIO_ACCOUNT_SID")
        self.twilio_token = twilio_token or os.environ.get("TWILIO_AUTH_TOKEN")
        self.from_number  = (from_number
                             or os.environ.get("TWILIO_WHATSAPP_FROM")
                             or self.DEFAULT_SANDBOX_FROM)
        if to_numbers is None:
            raw = os.environ.get("TWILIO_WHATSAPP_TO", "")
            to_numbers = [n.strip() for n in raw.split(",") if n.strip()]
        self.to_numbers = to_numbers

    def is_configured(self) -> bool:
        # Need creds + at least one recipient. From has a sandbox default so
        # we don't gate on it.
        return all([self.twilio_sid, self.twilio_token,
                    self.from_number, self.to_numbers])

    @staticmethod
    def _to_whatsapp_markdown(text: str) -> str:
        """Convert Telegram/Markdown link syntax `[label](url)` to plain
        `label: url` so WhatsApp doesn't render the brackets literally.
        Bold (`*x*`) + italic (`_x_`) work natively in both."""
        import re
        return re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1: \2', text)

    def send(self, subject: str, body: str,
             level: str = "info",
             metadata: Optional[dict] = None) -> dict:
        if not self.is_configured():
            return {"ok": False, "channel": self.name, "error": "not_configured"}

        full = f"*{subject}*\n\n{body}"
        full = self._to_whatsapp_markdown(full)
        # WhatsApp body limit is 1600 chars (same as SMS limit); keep margin
        if len(full) > 1500:
            full = full[:1497] + "..."

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_sid}/Messages.json"
        results = []
        for to in self.to_numbers:
            try:
                r = requests.post(
                    url,
                    auth=(self.twilio_sid, self.twilio_token),
                    data={
                        "From": f"whatsapp:{self.from_number}",
                        "To":   f"whatsapp:{to}",
                        "Body": full,
                    },
                    timeout=15,
                )
                r.raise_for_status()
                results.append({"to": to, "ok": True,
                                "sid": r.json().get("sid")})
            except requests.HTTPError as e:
                results.append({"to": to, "ok": False,
                                "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"})
            except Exception as e:
                results.append({"to": to, "ok": False, "error": str(e)})

        ok = all(r["ok"] for r in results)
        return {"ok": ok, "channel": self.name, "results": results}


# ─── Orchestrator ────────────────────────────────────────────────────────────

class Notifier:
    """Fans out alerts to all (or a subset of) configured channels.

    Tiered convenience (default — override TIER_ROUTING per-app if you like):
      send_tiered(s, b, conviction=1)  →  [telegram]
      send_tiered(s, b, conviction=2)  →  [telegram, whatsapp, email]
      send_tiered(s, b, conviction=3)  →  [telegram, whatsapp, email, sms]

    Rationale: Telegram is the lowest-noise channel (it's a bot, mute-friendly).
    WhatsApp + email escalate to "you should see this" without adding cost.
    SMS is reserved for highest-conviction alerts only since it costs money
    per send (especially internationally) — but YOU control the semantics by
    choosing the conviction value when you call send_tiered.
    """

    TIER_ROUTING = {
        1: ["telegram"],
        2: ["telegram", "whatsapp", "email"],
        3: ["telegram", "whatsapp", "email", "sms"],
    }

    def __init__(self, channels: Optional[list[NotificationChannel]] = None,
                 dedup_window_seconds: int = 600):
        self.channels = channels or []
        self.lock = threading.Lock()
        self._dedup_cache: dict[str, float] = {}
        self.dedup_window_seconds = dedup_window_seconds

    @classmethod
    def from_env(cls) -> "Notifier":
        """Auto-detect configured channels from env vars. Channels whose
        env vars aren't set return is_configured()=False and are silently
        skipped at send time — so it's safe to ship every channel here and
        let each deployment enable only what it has credentials for."""
        return cls(channels=[
            TelegramChannel(),
            WhatsAppChannel(),
            EmailChannel(),
            SMSChannel(),
        ])

    def configured(self) -> list[str]:
        return [c.name for c in self.channels if c.is_configured()]

    def send(self,
             subject: str,
             body:    str,
             channels: Optional[list[str]] = None,
             level:    str = "info",
             metadata: Optional[dict] = None,
             dedup_key: Optional[str] = None) -> dict:
        """Send to configured channels (optionally filtered by names).

        If dedup_key is provided, the same key won't re-fire within
        dedup_window_seconds. Useful so a sustained signal doesn't spam.
        """
        # De-dup
        if dedup_key:
            import time
            now = time.time()
            with self.lock:
                last = self._dedup_cache.get(dedup_key, 0)
                if now - last < self.dedup_window_seconds:
                    return {"ok": True, "skipped": "dedup",
                            "channel_results": []}
                self._dedup_cache[dedup_key] = now

        targets = [c for c in self.channels if c.is_configured()]
        if channels:
            chosen = set(channels)
            targets = [c for c in targets if c.name in chosen]

        if not targets:
            return {"ok": False, "channel_results": [],
                    "error": "no_channels_configured"}

        results = []
        for ch in targets:
            try:
                r = ch.send(subject, body, level=level, metadata=metadata)
            except Exception as e:
                r = {"ok": False, "channel": ch.name, "error": str(e)}
            results.append(r)

        any_ok = any(r.get("ok") for r in results)
        return {"ok": any_ok, "channel_results": results}

    def send_tiered(self,
                    subject:   str,
                    body:      str,
                    conviction: int = 1,
                    level:     str = "info",
                    metadata:  Optional[dict] = None,
                    dedup_key: Optional[str] = None) -> dict:
        """Convenience: tier 1 = telegram only, 2 = +email, 3 = +SMS."""
        conviction = max(1, min(3, int(conviction)))
        channels = self.TIER_ROUTING.get(conviction, ["telegram"])
        return self.send(subject, body,
                         channels=channels, level=level,
                         metadata=metadata, dedup_key=dedup_key)


# ─── NBA-specific signal formatter ───────────────────────────────────────────
# (Move this to a separate file if you want notifications.py purely generic.
#  Keeping it here for convenience since the radar is the primary consumer.)

def format_nba_signal(sig_record: dict) -> tuple[str, str]:
    """Convert a fired NBA Comeback Radar signal into (subject, body) text."""
    fire_kind  = sig_record.get("fire_kind", "?")
    n_signals  = int(sig_record.get("n_signals", 1) or 1)
    trailing   = sig_record.get("trailing", "?")
    leading    = sig_record.get("leading", "?")
    deficit    = sig_record.get("deficit", 0)
    quarter    = sig_record.get("quarter", "?")
    clock      = sig_record.get("clock", "")
    p_model    = sig_record.get("p_model")
    edge       = sig_record.get("edge")
    ev_pct     = sig_record.get("ev_pct")
    stake_pct  = sig_record.get("stake_pct")
    poly_event = sig_record.get("event_slug")
    reason     = sig_record.get("reason", "")

    # Symbol intensity by signal count
    star = "🔥🔥🔥" if n_signals >= 3 else ("🔥🔥" if n_signals >= 2 else "🔥")

    subject = (
        f"{star} {fire_kind.upper()}  "
        f"{trailing} -{deficit} (Q{quarter}{(' ' + clock) if clock else ''}) vs {leading}"
    )

    parts = [
        f"*Signal:* `{fire_kind}` ({n_signals} confirming)",
        f"*Trailing:* {trailing} down {deficit} in Q{quarter}{(' ' + clock) if clock else ''}",
        f"*Opponent:* {leading}",
        "",
    ]
    if p_model is not None:
        parts.append(f"*Model prob:* {p_model*100:.1f}%")
    if edge is not None:
        parts.append(f"*Edge:* {edge*100:+.1f}%")
    if ev_pct is not None:
        parts.append(f"*EV:* {ev_pct:+.1f}%")
    if stake_pct is not None:
        parts.append(f"*Stake:* {stake_pct*100:.1f}% of bankroll")
    parts.append("")
    if reason:
        parts.append(f"_{reason}_")
    if poly_event:
        parts.append(f"\n[Open on Polymarket](https://polymarket.com/event/{poly_event})")
    parts.append(f"\n→ [Live dashboard](https://nba.critterlabs.io)")

    return subject, "\n".join(parts)


# ─── NBA exit-alert formatter ────────────────────────────────────────────────

def format_exit_alert(exit_event: dict) -> tuple[str, str]:
    """Format an ExitEvent dict (from position_manager) for Telegram/Email/etc.

    Different exit types get different visual treatment so you can scan
    your alerts and instantly tell profit-take from emergency-bail.
    """
    kind         = exit_event.get("kind", "?")
    trailing     = exit_event.get("trailing_team", "?")
    leading      = exit_event.get("leading_team", "?")
    price        = exit_event.get("price", 0.0)
    entry_price  = exit_event.get("entry_price", 0.0)
    peak_price   = exit_event.get("peak_price", 0.0)
    pnl          = exit_event.get("pnl_contribution", 0.0)
    pnl_total    = exit_event.get("realized_pnl_total", 0.0)
    fraction     = exit_event.get("fraction_sold", 0.0)
    open_after   = exit_event.get("open_fraction_after", 0.0)
    quarter      = exit_event.get("quarter")
    deficit      = exit_event.get("deficit")

    # Visual treatment per exit kind
    icons = {
        "T1":           "💰",
        "T2":           "💰💰",
        "trail":        "🎯",
        "stop_loss":    "🛑",
        "deficit_kill": "☠️",
        "settlement":   "🏁",
    }
    labels = {
        "T1":           "T1 PROFIT-TAKE",
        "T2":           "T2 PROFIT-TAKE",
        "trail":        "TRAIL STOP",
        "stop_loss":    "STOP-LOSS",
        "deficit_kill": "DEFICIT KILL",
        "settlement":   "SETTLEMENT",
    }
    icon  = icons.get(kind, "📤")
    label = labels.get(kind, kind.upper())

    pct_total = (pnl_total / entry_price * 100) if entry_price > 0 else 0
    state_str = ""
    if quarter and deficit is not None:
        sign = "-" if deficit > 0 else "+" if deficit < 0 else ""
        state_str = f" (Q{quarter}, {trailing} {sign}{abs(deficit)})"

    subject = (
        f"{icon} {label}: {trailing} @ {price*100:.0f}¢{state_str}"
    )

    parts = [
        f"*{trailing} vs {leading}* — {label}",
        f"*Sell:* {fraction*100:.0f}% of position at *{price*100:.0f}¢*",
        f"*Entry:* {entry_price*100:.0f}¢   *Peak:* {peak_price*100:.0f}¢",
        f"*This exit:* {pnl*100:+.1f}¢/share",
        f"*Cumulative realized:* {pnl_total*100:+.1f}¢/share ({pct_total:+.0f}% of entry cost)",
    ]
    if open_after > 0:
        parts.append(f"*Position remaining:* {open_after*100:.0f}% (still open)")
    else:
        parts.append("*Position fully closed.*")
    parts.append("\n→ [Live dashboard](https://nba.critterlabs.io)")

    return subject, "\n".join(parts)


# ─── Smoke test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Try loading from secrets.json for local testing
    secrets_path = os.path.join(os.path.dirname(__file__), "secrets.json")
    if os.path.exists(secrets_path):
        try:
            secrets = json.load(open(secrets_path))
            for k, v in secrets.items():
                os.environ.setdefault(k, str(v))
        except Exception as e:
            print(f"[warn] secrets.json load failed: {e}")

    notifier = Notifier.from_env()
    configured = notifier.configured()
    print(f"\nConfigured channels: {configured or '(none)'}")
    if not configured:
        print("\nSet env vars (or secrets.json) to enable channels.\n"
              "See module docstring for each channel's setup.")
        raise SystemExit(0)

    print("\nSending test alert...")
    result = notifier.send(
        "🧪 Test alert from notifications.py",
        "If you see this, your channel is wired up correctly.\n\n"
        "_This was a Notifier.send() smoke test._",
        level="info",
    )
    print(f"\nResult: {json.dumps(result, indent=2)}")
