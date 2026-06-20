"""Tests for Slack and MS Teams webhook notification channels."""
import pytest
from unittest.mock import patch, MagicMock
from django.test import override_settings
from apps.alerts.channels.webhook import (
    send_slack_alert,
    send_slack_recovery,
    send_teams_alert,
    send_teams_recovery,
)


@pytest.fixture
def mock_alert():
    alert = MagicMock()
    alert.id = 42
    alert.severity = "CRITICAL"
    alert.device.name = "sw-core-01"
    alert.device.ip_address = "10.0.0.1"
    alert.rule.name = "CPU High"
    alert.message = "sw-core-01: cpu_percent = 92.5 (ngưỡng gt 80.0)"
    alert.triggered_at.strftime.return_value = "22:45:00 26/05/2026"
    alert.resolved_at.strftime.return_value = "22:50:00 26/05/2026"
    return alert


class TestWebhookChannels:
    @patch("apps.alerts.channels.webhook.requests.post")
    @override_settings(SLACK_WEBHOOK_URL="")
    def test_send_slack_alert_missing_setting(self, mock_post, mock_alert):
        send_slack_alert(mock_alert)
        mock_post.assert_not_called()

    @patch("apps.alerts.channels.webhook.requests.post")
    @override_settings(SLACK_WEBHOOK_URL="https://slack.mock/webhooks/123")
    def test_send_slack_alert_success(self, mock_post, mock_alert):
        send_slack_alert(mock_alert)
        mock_post.assert_called_once_with(
            "https://slack.mock/webhooks/123",
            json={"text": "🔴 *CRITICAL Alert*\n*Thiết bị:* sw-core-01 (10.0.0.1)\n*Rule:* CPU High\n*Chi tiết:* sw-core-01: cpu_percent = 92.5 (ngưỡng gt 80.0)\n*Thời gian:* 22:45:00 26/05/2026 UTC"},
            timeout=10,
        )

    @patch("apps.alerts.channels.webhook.requests.post")
    @override_settings(SLACK_WEBHOOK_URL="https://slack.mock/webhooks/123")
    def test_send_slack_recovery_success(self, mock_post, mock_alert):
        send_slack_recovery(mock_alert)
        mock_post.assert_called_once_with(
            "https://slack.mock/webhooks/123",
            json={"text": "✅ *RECOVERED*\n*Thiết bị:* sw-core-01 (10.0.0.1)\n*Rule:* CPU High\n*Hồi phục lúc:* 22:50:00 26/05/2026 UTC"},
            timeout=10,
        )

    @patch("apps.alerts.channels.webhook.requests.post")
    @override_settings(TEAMS_WEBHOOK_URL="https://teams.mock/webhooks/456")
    def test_send_teams_alert_success(self, mock_post, mock_alert):
        send_teams_alert(mock_alert)
        mock_post.assert_called_once_with(
            "https://teams.mock/webhooks/456",
            json={"text": "### 🔴 CRITICAL Alert\n**Thiết bị:** sw-core-01 (10.0.0.1)\n**Rule:** CPU High\n**Chi tiết:** sw-core-01: cpu_percent = 92.5 (ngưỡng gt 80.0)\n**Thời gian:** 22:45:00 26/05/2026 UTC"},
            timeout=10,
        )

    @patch("apps.alerts.channels.webhook.requests.post")
    @override_settings(TEAMS_WEBHOOK_URL="https://teams.mock/webhooks/456")
    def test_send_teams_recovery_success(self, mock_post, mock_alert):
        send_teams_recovery(mock_alert)
        mock_post.assert_called_once_with(
            "https://teams.mock/webhooks/456",
            json={"text": "### ✅ RECOVERED\n**Thiết bị:** sw-core-01 (10.0.0.1)\n**Rule:** CPU High\n**Hồi phục lúc:** 22:50:00 26/05/2026 UTC"},
            timeout=10,
        )
