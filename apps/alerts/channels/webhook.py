"""Gửi alert qua Slack và Microsoft Teams Webhook API."""
import hashlib
import logging

from django.conf import settings

from . import ChannelNotConfigured, port_context, post_json

logger = logging.getLogger(__name__)


def _webhook_url(setting: str) -> str:
    """URL webhook hoặc ChannelNotConfigured (trước đây `return` im lặng → engine ghi "sent" giả).
    URL webhook là bí mật nên thông điệp chỉ nêu TÊN biến cấu hình."""
    url = getattr(settings, setting, "")
    if not url:
        raise ChannelNotConfigured(f"Chưa cấu hình {setting}")
    return url


_URL_SETTING = {"slack": "SLACK_WEBHOOK_URL", "teams": "TEAMS_WEBHOOK_URL"}


def cooldown_key(channel: str) -> str | None:
    """Khoá cooldown dùng chung theo webhook (xem apps/alerts/cooldown.py): băm URL vì URL là bí mật."""
    url = getattr(settings, _URL_SETTING.get(channel, ""), "")
    if not url:
        return None
    return f"{channel}:{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"


def send_slack_alert(alert) -> None:
    webhook_url = _webhook_url("SLACK_WEBHOOK_URL")

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    text = (
        f"{emoji} *{alert.severity} Alert*\n"
        f"*Thiết bị:* {alert.device.name} ({alert.device.ip_address})\n"
        f"*Rule:* {alert.rule.name}\n"
        f"*Chi tiết:* {alert.message}\n"
        f"*Thời gian:* {alert.triggered_at.strftime('%H:%M:%S %d/%m/%Y')} UTC"
    )

    post_json(webhook_url, {"text": text}, service="Slack")
    logger.info("Slack alert sent for alert %s", alert.id)


def send_slack_recovery(alert) -> None:
    webhook_url = _webhook_url("SLACK_WEBHOOK_URL")

    resolved_str = (alert.resolved_at.strftime("%H:%M:%S %d/%m/%Y")
                    if alert.resolved_at else "N/A")
    port_name, peer_name = port_context(alert)
    port_lines = ""
    if port_name:
        port_lines = f"*Cổng:* {port_name}\n"
        if peer_name:
            port_lines += f"*Nối tới:* {peer_name}\n"
    text = (
        f"✅ *RECOVERED*\n"
        f"*Thiết bị:* {alert.device.name} ({alert.device.ip_address})\n"
        f"{port_lines}"
        f"*Rule:* {alert.rule.name}\n"
        f"*Hồi phục lúc:* {resolved_str} UTC"
    )

    post_json(webhook_url, {"text": text}, service="Slack")
    logger.info("Slack recovery sent for alert %s", alert.id)


def send_teams_alert(alert) -> None:
    webhook_url = _webhook_url("TEAMS_WEBHOOK_URL")

    emoji = "🔴" if alert.severity == "CRITICAL" else "⚠️"
    text = (
        f"### {emoji} {alert.severity} Alert\n"
        f"**Thiết bị:** {alert.device.name} ({alert.device.ip_address})\n"
        f"**Rule:** {alert.rule.name}\n"
        f"**Chi tiết:** {alert.message}\n"
        f"**Thời gian:** {alert.triggered_at.strftime('%H:%M:%S %d/%m/%Y')} UTC"
    )

    post_json(webhook_url, {"text": text}, service="Teams")
    logger.info("Teams alert sent for alert %s", alert.id)


def send_teams_recovery(alert) -> None:
    webhook_url = _webhook_url("TEAMS_WEBHOOK_URL")

    resolved_str = (alert.resolved_at.strftime("%H:%M:%S %d/%m/%Y")
                    if alert.resolved_at else "N/A")
    port_name, peer_name = port_context(alert)
    port_lines = ""
    if port_name:
        port_lines = f"**Cổng:** {port_name}\n"
        if peer_name:
            port_lines += f"**Nối tới:** {peer_name}\n"
    text = (
        f"### ✅ RECOVERED\n"
        f"**Thiết bị:** {alert.device.name} ({alert.device.ip_address})\n"
        f"{port_lines}"
        f"**Rule:** {alert.rule.name}\n"
        f"**Hồi phục lúc:** {resolved_str} UTC"
    )

    post_json(webhook_url, {"text": text}, service="Teams")
    logger.info("Teams recovery sent for alert %s", alert.id)
