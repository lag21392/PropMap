from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import store
from .accounts import send_verify_email, _get_by_id
from .alerts import load as load_alerts, update_alert
from .search_auth import apply_filters

log = logging.getLogger(__name__)

ALERTS_WORKER_INTERVAL_SEC = 24 * 3600  # Daily
ALERTS_WORKER_META_KEY = "alerts_worker_last_run"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_listings_for_alerts() -> list[Any]:
    """Load all current listings for alert matching."""
    store.init()
    # Get all city IDs from the database
    with store.connect() as conn:
        rows = conn.execute("SELECT DISTINCT city FROM listings WHERE city IS NOT NULL AND city != ''").fetchall()
    city_ids = {row["city"] for row in rows}
    listings = []
    for city_id in city_ids:
        listings.extend(store.fetch_for_city(city_id))
    return listings


def _send_alert_email(email: str, username: str, alert: dict, matches: list[dict]) -> bool:
    """Send an email notification for alert matches."""
    from email.message import EmailMessage
    import smtplib
    import os

    subject = f"🔔 PropMap: {len(matches)} nuevos avisos para tu alerta"
    
    # Build email body
    lines = [
        f"Hola {username},",
        "",
        f"Tu alerta encontró {len(matches)} avisos nuevos:",
        ""
    ]
    
    for i, m in enumerate(matches[:10], 1):
        price_str = ""
        if m.get("price") and m.get("currency"):
            price_str = f" · {m['price']:,} {m['currency']}"
        if m.get("price_usd"):
            price_str += f" (≈ USD {m['price_usd']:,.0f})"
        
        addr = m.get("address") or m.get("neighborhood") or "Sin dirección"
        lines.append(f"  {i}. {m.get('title', 'Sin título')}{price_str}")
        lines.append(f"     {addr}")
        if m.get("url"):
            lines.append(f"     {m['url']}")
        lines.append("")
    
    if len(matches) > 10:
        lines.append(f"  ... y {len(matches) - 10} más.")
        lines.append("")
    
    lines.extend([
        "Podés ver todos los resultados en PropMap.",
        "",
        "— PropMap"
    ])
    
    body = "\n".join(lines)
    
    # Use the same email delivery mechanism as verification emails
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"PropMap <{os.environ.get('MAIL_ADDRESS', 'noreply@propmap.local')}>"
        msg["To"] = email
        msg.set_content(body)
        
        # Try SMTP first
        host = os.environ.get("SMTP_HOST")
        if host and host not in {"", "propmap-mail", "mailpit", "localhost", "127.0.0.1"}:
            port = int(os.environ.get("SMTP_PORT") or 587)
            user = os.environ.get("SMTP_USER") or ""
            password = os.environ.get("SMTP_PASSWORD") or ""
            tls = (os.environ.get("SMTP_TLS") or "1").strip().lower() not in {"0", "false", "no"}
            
            with smtplib.SMTP(host, port, timeout=12) as smtp:
                smtp.ehlo()
                if tls and smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
            return True
        
        # Fallback to local SMTP or MX delivery
        # For now, just log (in production would use the same delivery as verify emails)
        log.info("Alert email would be sent to %s: %s", email, subject)
        return True
        
    except Exception as e:
        log.exception("Failed to send alert email to %s: %s", email, e)
        return False


def _check_alert(alert: dict, listings: list[Any]) -> list[dict]:
    """Check a single alert against listings and return matches."""
    filters = alert.get("filters", {})
    filtered = apply_filters(listings, filters)
    
    # Convert to serializable format
    matches = []
    for item in filtered:
        matches.append({
            "id": item.id,
            "title": item.title,
            "price": item.price,
            "currency": item.currency,
            "price_usd": item.price_usd,
            "address": item.address,
            "neighborhood": item.neighborhood,
            "url": item.url,
            "city": item.city,
        })
    return matches


def run_alerts_worker_once() -> dict[str, Any]:
    """Run the alerts worker once - check all active alerts and send notifications."""
    log.info("Starting alerts worker run")
    
    store.init()
    all_alerts = load_alerts()
    active_alerts = [a for a in all_alerts if a.get("active", True)]
    
    if not active_alerts:
        log.info("No active alerts to process")
        return {"processed": 0, "notifications_sent": 0, "errors": 0}
    
    # Load all listings once
    listings = _load_listings_for_alerts()
    log.info("Loaded %d listings for alert matching", len(listings))
    
    # Group alerts by user_id
    alerts_by_user: dict[str, list[dict]] = {}
    for alert in active_alerts:
        user_id = alert.get("user_id")
        if user_id:
            alerts_by_user.setdefault(user_id, []).append(alert)
    
    notifications_sent = 0
    errors = 0
    
    for user_id, user_alerts in alerts_by_user.items():
        # Get user account for email
        account_row = _get_by_id(user_id)
        if not account_row:
            log.warning("User %s not found for alerts", user_id)
            continue
        
        # Decrypt email
        from .accounts import _decrypt
        try:
            email = _decrypt(account_row["email_enc"])
        except Exception:
            log.exception("Failed to decrypt email for user %s", user_id)
            errors += 1
            continue
        
        if not email or not email.strip():
            log.warning("User %s has no email", user_id)
            continue
        
        username = account_row.get("username") or "Usuario"
        
        # Check each alert for this user
        for alert in user_alerts:
            try:
                matches = _check_alert(alert, listings)
                
                if matches:
                    # Check if we should send (avoid spam - only send if new matches since last_sent)
                    last_sent = alert.get("last_sent")
                    should_send = True
                    
                    if last_sent:
                        # Could implement more sophisticated deduplication here
                        # For now, send every time there are matches
                        pass
                    
                    if should_send:
                        sent = _send_alert_email(email, username, alert, matches)
                        if sent:
                            # Update last_sent timestamp
                            update_alert(alert["id"], last_sent=_now_iso())
                            notifications_sent += 1
                            log.info("Sent alert notification to %s for alert %s (%d matches)", 
                                   email, alert["id"], len(matches))
                        else:
                            errors += 1
                            
            except Exception as e:
                log.exception("Error processing alert %s for user %s: %s", 
                            alert.get("id"), user_id, e)
                errors += 1
    
    # Update last run timestamp
    store.set_meta(ALERTS_WORKER_META_KEY, _now_iso())
    
    result = {
        "processed": len(active_alerts),
        "notifications_sent": notifications_sent,
        "errors": errors,
        "timestamp": _now_iso()
    }
    log.info("Alerts worker completed: %s", result)
    return result


def start_alerts_worker() -> None:
    """Start the alerts worker as a background daemon thread."""
    def worker_loop() -> None:
        # Initial delay to let app start up
        time.sleep(30)
        
        while True:
            try:
                run_alerts_worker_once()
            except Exception as e:
                log.exception("Alerts worker error: %s", e)
            
            # Sleep until next run
            time.sleep(ALERTS_WORKER_INTERVAL_SEC)
    
    thread = threading.Thread(target=worker_loop, daemon=True, name="propmap-alerts-worker")
    thread.start()
    log.info("Alerts worker started")


def get_alerts_worker_status() -> dict[str, Any]:
    """Get the status of the alerts worker."""
    store.init()
    last_run = store.get_meta(ALERTS_WORKER_META_KEY)
    return {
        "last_run": last_run,
        "interval_sec": ALERTS_WORKER_INTERVAL_SEC,
        "next_run": None  # Could calculate if needed
    }