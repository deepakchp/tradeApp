"""
modules/notifier.py — Trade Email Notifications
================================================
Sends email alerts when trades are executed (exits, adjustments).
Credentials are read from environment variables — NEVER hardcoded.

Required env vars:
    EMAIL_SENDER       — sender Gmail address
    EMAIL_APP_PASSWORD — Gmail App Password (not your main password)
    EMAIL_RECIPIENT    — recipient email address

Optional:
    EMAIL_ENABLED      — set to "false" to disable (default: "true")
    EMAIL_SMTP_HOST    — SMTP host (default: smtp.gmail.com)
    EMAIL_SMTP_PORT    — SMTP port (default: 587)
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────
# CONFIG (from environment)
# ─────────────────────────────────────────────────────────────────
EMAIL_ENABLED    = os.getenv("EMAIL_ENABLED", "true").lower() == "true"
EMAIL_SENDER     = os.getenv("EMAIL_SENDER", "")
EMAIL_APP_PWD    = os.getenv("EMAIL_APP_PASSWORD", "")
EMAIL_RECIPIENT  = os.getenv("EMAIL_RECIPIENT", "")
SMTP_HOST        = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT        = int(os.getenv("EMAIL_SMTP_PORT", "587"))


def _is_configured() -> bool:
    """Check all required env vars are set."""
    if not EMAIL_ENABLED:
        return False
    missing = []
    if not EMAIL_SENDER:
        missing.append("EMAIL_SENDER")
    if not EMAIL_APP_PWD:
        missing.append("EMAIL_APP_PASSWORD")
    if not EMAIL_RECIPIENT:
        missing.append("EMAIL_RECIPIENT")
    if missing:
        log.warning("notifier.not_configured", missing_env_vars=missing)
        return False
    return True


def _send_email(subject: str, html_body: str) -> bool:
    """Send an HTML email via SMTP. Returns True on success."""
    if not _is_configured():
        return False

    msg = MIMEMultipart("alternative")
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_APP_PWD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())
        log.info("notifier.email_sent", subject=subject, to=EMAIL_RECIPIENT)
        return True
    except Exception as exc:
        log.error("notifier.email_failed", error=str(exc), subject=subject)
        return False


# ─────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────

def notify_position_exit(
    position_id: str,
    reason: str,
    symbol: str,
    net_pnl: float,
    fills: List[Dict[str, Any]],
) -> bool:
    """Send email when a position is closed."""
    now = datetime.now().strftime("%d-%b-%Y %H:%M:%S IST")

    fills_rows = ""
    for f in fills:
        mode_badge = (
            '<span style="color:#ffc107;">PAPER</span>'
            if f.get("mode") == "paper"
            else '<span style="color:#00e676;">LIVE</span>'
        )
        fills_rows += f"""
        <tr>
            <td>{f.get('leg', '-')}</td>
            <td>{f.get('side', '-')}</td>
            <td>₹{f.get('fill_price', 0):.2f}</td>
            <td>{f.get('chase_steps', '-')}</td>
            <td>{mode_badge}</td>
        </tr>"""

    pnl_color = "#00e676" if net_pnl >= 0 else "#ff5252"

    html = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; background:#1a1a2e; color:#e0e0e0; padding:24px; border-radius:8px; max-width:600px;">
        <h2 style="color:#00e676; margin-top:0;">Position Closed</h2>
        <table style="width:100%; border-collapse:collapse; margin-bottom:16px;">
            <tr><td style="padding:6px 0; color:#888;">Position ID</td><td><b>{position_id}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Symbol</td><td><b>{symbol}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Reason</td><td>{reason}</td></tr>
            <tr><td style="padding:6px 0; color:#888;">Net P&L</td><td style="color:{pnl_color}; font-size:18px;"><b>₹{net_pnl:,.2f}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Time</td><td>{now}</td></tr>
        </table>

        <h3 style="color:#bb86fc; margin-bottom:8px;">Fills</h3>
        <table style="width:100%; border-collapse:collapse; font-size:13px;">
            <tr style="border-bottom:1px solid #333;">
                <th style="text-align:left; padding:6px; color:#888;">Leg</th>
                <th style="text-align:left; padding:6px; color:#888;">Side</th>
                <th style="text-align:left; padding:6px; color:#888;">Price</th>
                <th style="text-align:left; padding:6px; color:#888;">Chase</th>
                <th style="text-align:left; padding:6px; color:#888;">Mode</th>
            </tr>
            {fills_rows}
        </table>

        <p style="color:#555; font-size:11px; margin-top:20px;">VRP Trading System • Auto-generated notification</p>
    </div>
    """

    subject = f"{'🟢' if net_pnl >= 0 else '🔴'} Exit: {symbol} | ₹{net_pnl:,.2f} | {reason}"
    return _send_email(subject, html)


def notify_adjustment(
    position_id: str,
    action: str,
    symbol: str,
    leg_symbol: Optional[str] = None,
    target_delta: Optional[float] = None,
    details: Optional[Dict] = None,
) -> bool:
    """Send email when a position is adjusted."""
    now = datetime.now().strftime("%d-%b-%Y %H:%M:%S IST")

    action_labels = {
        "roll_challenged_leg": "Roll Challenged Leg",
        "roll_untested_leg":   "Roll Untested Leg",
        "roll_out":            "ITM Breach Roll-Out",
        "low_vol_adjust":      "Low-Vol Regime Adjustment",
        "close_all":           "Impulsive Move — Close All",
    }
    action_display = action_labels.get(action, action)

    details_rows = ""
    if details:
        for k, v in details.items():
            details_rows += f'<tr><td style="padding:4px 0; color:#888;">{k}</td><td>{v}</td></tr>'

    html = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; background:#1a1a2e; color:#e0e0e0; padding:24px; border-radius:8px; max-width:600px;">
        <h2 style="color:#ffc107; margin-top:0;">Position Adjusted</h2>
        <table style="width:100%; border-collapse:collapse; margin-bottom:16px;">
            <tr><td style="padding:6px 0; color:#888;">Position ID</td><td><b>{position_id}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Symbol</td><td><b>{symbol}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Action</td><td style="color:#ffc107;"><b>{action_display}</b></td></tr>
            <tr><td style="padding:6px 0; color:#888;">Leg</td><td>{leg_symbol or '—'}</td></tr>
            <tr><td style="padding:6px 0; color:#888;">Target Delta</td><td>{f'{target_delta:.2f}' if target_delta else '—'}</td></tr>
            <tr><td style="padding:6px 0; color:#888;">Time</td><td>{now}</td></tr>
        </table>

        {"<h3 style='color:#bb86fc; margin-bottom:8px;'>Details</h3><table style='width:100%; font-size:13px;'>" + details_rows + "</table>" if details_rows else ""}

        <p style="color:#555; font-size:11px; margin-top:20px;">VRP Trading System • Auto-generated notification</p>
    </div>
    """

    subject = f"⚡ Adjust: {symbol} | {action_display}" + (f" | {leg_symbol}" if leg_symbol else "")
    return _send_email(subject, html)
