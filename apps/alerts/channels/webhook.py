"""Gửi alert qua Slack và Microsoft Teams Webhook API."""
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_slack_alert(alert) -> None:
    webhook_url = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL chưa cấu hình")
        return

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    text = (
        f"{emoji} *{alert.severity} Alert*\n"
        f"*Thiết bị:* {alert.device.name} ({alert.device.ip_address})\n"
        f"*Rule:* {alert.rule.name}\n"
        f"*Chi tiết:* {alert.message}\n"
        f"*Thời gian:* {alert.triggered_at.strftime('%H:%M:%S %d/%m/%Y')} UTC"
    )

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()
    logger.info("Slack alert sent for alert %s", alert.id)


def send_slack_recovery(alert) -> None:
    webhook_url = getattr(settings, "SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        return

    resolved_str = (alert.resolved_at.strftime("%H:%M:%S %d/%m/%Y")
                    if alert.resolved_at else "N/A")
    text = (
        f"✅ *RECOVERED*\n"
        f"*Thiết bị:* {alert.device.name} ({alert.device.ip_address})\n"
        f"*Rule:* {alert.rule.name}\n"
        f"*Hồi phục lúc:* {resolved_str} UTC"
    )

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()
    logger.info("Slack recovery sent for alert %s", alert.id)


def send_teams_alert(alert) -> None:
    webhook_url = getattr(settings, "TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        logger.warning("TEAMS_WEBHOOK_URL chưa cấu hình")
        return

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    text = (
        f"### {emoji} {alert.severity} Alert\n"
        f"**Thiết bị:** {alert.device.name} ({alert.device.ip_address})\n"
        f"**Rule:** {alert.rule.name}\n"
        f"**Chi tiết:** {alert.message}\n"
        f"**Thời gian:** {alert.triggered_at.strftime('%H:%M:%S %d/%m/%Y')} UTC"
    )

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()
    logger.info("Teams alert sent for alert %s", alert.id)


def send_teams_recovery(alert) -> None:
    webhook_url = getattr(settings, "TEAMS_WEBHOOK_URL", "")
    if not webhook_url:
        return

    resolved_str = (alert.resolved_at.strftime("%H:%M:%S %d/%m/%Y")
                    if alert.resolved_at else "N/A")
    text = (
        f"### ✅ RECOVERED\n"
        f"**Thiết bị:** {alert.device.name} ({alert.device.ip_address})\n"
        f"**Rule:** {alert.rule.name}\n"
        f"**Hồi phục lúc:** {resolved_str} UTC"
    )

    resp = requests.post(webhook_url, json={"text": text}, timeout=10)
    resp.raise_for_status()
    logger.info("Teams recovery sent for alert %s", alert.id)
