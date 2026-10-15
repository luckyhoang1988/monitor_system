"""Phân loại lỗi gửi thông báo ở tầng channel (review 2026-10-15, hạng mục 1.2).

Số đo prod (309 thông báo failed): 429 rate-limit 172, lỗi kết nối/pool 124, SoftTimeLimit 12, 4xx 1 —
trước đây MỌI lỗi đều thành `failed` cuối cùng nên lỗi tạm thời làm mất thông báo. Channel phải nói rõ lỗi
nào là tạm thời (thử lại được, kèm retry_after nếu có), lỗi nào vĩnh viễn, lỗi nào là chưa cấu hình — và
thông điệp lỗi KHÔNG được chứa token/URL webhook (requests nhét nguyên URL `/bot<token>/` vào str(exc)).
"""
import smtplib
import socket
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from django.test import override_settings

from apps.alerts.channels import (
    ChannelNotConfigured,
    ChannelPermanentError,
    ChannelTransientError,
    redact,
)
from apps.alerts.channels.email_channel import send_email_alert, send_email_recovery
from apps.alerts.channels.telegram import send_telegram_alert, send_telegram_recovery
from apps.alerts.channels.webhook import send_slack_alert, send_teams_alert

TOKEN = "123456789:AAH-secretTokenValue_xyz"
SLACK_URL = "https://hooks.slack.test/services/T000/B000/SECRETSECRET"


def make_alert():
    return SimpleNamespace(
        id=7, severity="CRITICAL",
        device=SimpleNamespace(pk=3, name="sw-1", ip_address="10.0.0.1", device_type="switch"),
        rule=SimpleNamespace(name="CPU High"),
        message="sw-1: CPU = 99",
        triggered_at=datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 10, 15, 3, 5, tzinfo=timezone.utc),
    )


def fake_resp(status, body=None, headers=None):
    return SimpleNamespace(
        status_code=status, ok=status < 400, headers=headers or {},
        json=lambda: body if body is not None else (_ for _ in ()).throw(ValueError("no json")),
    )


@pytest.fixture
def tg_settings():
    with override_settings(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="-1001234"):
        yield


@pytest.mark.django_db
@pytest.mark.usefixtures("tg_settings")
class TestTelegramErrors:
    def test_success_does_not_raise(self, mocker):
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(200, {"ok": True}))
        send_telegram_alert(make_alert())
        send_telegram_recovery(make_alert())

    def test_429_is_transient_and_carries_retry_after_from_body(self, mocker):
        body = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 35",
                "parameters": {"retry_after": 35}}
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(429, body))
        with pytest.raises(ChannelTransientError) as ei:
            send_telegram_alert(make_alert())
        assert ei.value.retry_after == 35
        assert TOKEN not in str(ei.value)
        assert "429" in str(ei.value)

    def test_429_without_body_is_still_transient(self, mocker):
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(429))
        with pytest.raises(ChannelTransientError) as ei:
            send_telegram_recovery(make_alert())
        assert ei.value.retry_after is None

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_is_transient(self, mocker, status):
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(status))
        with pytest.raises(ChannelTransientError):
            send_telegram_alert(make_alert())

    @pytest.mark.parametrize("exc", [
        requests.ConnectionError(f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
                                 f"exceeded with url: /bot{TOKEN}/sendMessage (Caused by NewConnectionError)"),
        requests.Timeout(f"Read timed out. (read timeout=10) url=/bot{TOKEN}/sendMessage"),
    ])
    def test_network_errors_are_transient_and_do_not_leak_token(self, mocker, exc):
        mocker.patch("apps.alerts.channels.requests.post", side_effect=exc)
        with pytest.raises(ChannelTransientError) as ei:
            send_telegram_alert(make_alert())
        assert TOKEN not in str(ei.value)
        assert "bot" + TOKEN.split(":")[0] not in str(ei.value)

    def test_400_is_permanent_with_telegram_description(self, mocker):
        body = {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(400, body))
        with pytest.raises(ChannelPermanentError) as ei:
            send_telegram_alert(make_alert())
        assert "chat not found" in str(ei.value)
        assert TOKEN not in str(ei.value)

    def test_group_migrated_to_supergroup_is_permanent_with_hint(self, mocker):
        body = {"ok": False, "error_code": 400,
                "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1009999}}
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(400, body))
        with pytest.raises(ChannelPermanentError) as ei:
            send_telegram_alert(make_alert())
        assert "supergroup" in str(ei.value).lower()

    def test_missing_token_or_chat_is_not_configured_not_silent(self):
        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="-1001234"):
            with pytest.raises(ChannelNotConfigured):
                send_telegram_alert(make_alert())
            with pytest.raises(ChannelNotConfigured):
                send_telegram_recovery(make_alert())
        with override_settings(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=""):
            with pytest.raises(ChannelNotConfigured):
                send_telegram_alert(make_alert())


class TestWebhookErrors:
    @override_settings(SLACK_WEBHOOK_URL="")
    def test_missing_webhook_url_is_not_configured(self):
        with pytest.raises(ChannelNotConfigured):
            send_slack_alert(make_alert())

    @override_settings(TEAMS_WEBHOOK_URL="")
    def test_missing_teams_url_is_not_configured(self):
        with pytest.raises(ChannelNotConfigured):
            send_teams_alert(make_alert())

    @override_settings(SLACK_WEBHOOK_URL=SLACK_URL)
    def test_429_honours_retry_after_header(self, mocker):
        mocker.patch("apps.alerts.channels.requests.post",
                     return_value=fake_resp(429, headers={"Retry-After": "12"}))
        with pytest.raises(ChannelTransientError) as ei:
            send_slack_alert(make_alert())
        assert ei.value.retry_after == 12

    @override_settings(SLACK_WEBHOOK_URL=SLACK_URL)
    def test_network_error_does_not_leak_webhook_url(self, mocker):
        mocker.patch("apps.alerts.channels.requests.post",
                     side_effect=requests.ConnectionError(f"Max retries exceeded with url: {SLACK_URL}"))
        with pytest.raises(ChannelTransientError) as ei:
            send_slack_alert(make_alert())
        assert "SECRETSECRET" not in str(ei.value)

    @override_settings(SLACK_WEBHOOK_URL=SLACK_URL)
    def test_404_is_permanent(self, mocker):
        mocker.patch("apps.alerts.channels.requests.post", return_value=fake_resp(404))
        with pytest.raises(ChannelPermanentError) as ei:
            send_slack_alert(make_alert())
        assert "SECRETSECRET" not in str(ei.value)


class TestEmailErrors:
    @override_settings(ALERT_EMAIL_RECIPIENTS=[])
    def test_no_recipients_is_not_configured(self):
        with pytest.raises(ChannelNotConfigured):
            send_email_alert(make_alert())
        with pytest.raises(ChannelNotConfigured):
            send_email_recovery(make_alert())

    @override_settings(ALERT_EMAIL_RECIPIENTS=["ops@example.test"], SMTP_FROM="m@example.test")
    @pytest.mark.parametrize("exc", [
        smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
        socket.timeout("timed out"),
        ConnectionRefusedError("refused"),
    ])
    def test_connection_level_smtp_errors_are_transient(self, mocker, exc):
        mocker.patch("apps.alerts.channels.email_channel.send_mail", side_effect=exc)
        with pytest.raises(ChannelTransientError):
            send_email_alert(make_alert())

    @override_settings(ALERT_EMAIL_RECIPIENTS=["ops@example.test"], SMTP_FROM="m@example.test")
    def test_smtp_4xx_reply_is_transient_but_auth_failure_is_not(self, mocker):
        mocker.patch("apps.alerts.channels.email_channel.send_mail",
                     side_effect=smtplib.SMTPResponseException(451, b"try again later"))
        with pytest.raises(ChannelTransientError):
            send_email_alert(make_alert())
        mocker.patch("apps.alerts.channels.email_channel.send_mail",
                     side_effect=smtplib.SMTPAuthenticationError(535, b"bad credentials"))
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send_email_alert(make_alert())


class TestEmailRecipientsRefused:
    """Review 2026-10-15 (P2): mọi người nhận bị từ chối bằng mã 450 → smtplib ném SMTPRecipientsRefused,
    KHÔNG phải SMTPResponseException. Nhánh cũ ném lại → engine ghi `failed` và không thử nữa dù là lỗi
    tạm thời (hộp thư bận, greylisting). Phải xét mã trong exc.recipients."""

    @pytest.fixture(autouse=True)
    def email_settings(self, settings):
        settings.ALERT_EMAIL_RECIPIENTS = ["ops@example.test", "noc@example.test"]
        settings.SMTP_FROM = "m@example.test"

    @pytest.mark.parametrize("recipients", [
        {"ops@example.test": (450, b"mailbox busy"), "noc@example.test": (451, b"greylisted")},
        {"ops@example.test": (421, b"service not available")},
        {"ops@example.test": (450, b"mailbox busy"), "noc@example.test": (550, b"no such user")},  # lẫn
    ])
    def test_any_4xx_refusal_is_transient(self, mocker, recipients):
        mocker.patch("apps.alerts.channels.email_channel.send_mail",
                     side_effect=smtplib.SMTPRecipientsRefused(recipients))
        with pytest.raises(ChannelTransientError):
            send_email_alert(make_alert())
        with pytest.raises(ChannelTransientError):
            send_email_recovery(make_alert())

    def test_all_5xx_refusals_stay_permanent(self, mocker):
        mocker.patch("apps.alerts.channels.email_channel.send_mail", side_effect=smtplib.SMTPRecipientsRefused(
            {"ops@example.test": (550, b"no such user"), "noc@example.test": (553, b"bad mailbox name")}))
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            send_email_alert(make_alert())

    def test_malformed_recipients_payload_is_permanent_not_a_crash(self, mocker):
        mocker.patch("apps.alerts.channels.email_channel.send_mail",
                     side_effect=smtplib.SMTPRecipientsRefused({"ops@example.test": "???"}))
        with pytest.raises(smtplib.SMTPRecipientsRefused):
            send_email_alert(make_alert())


class TestRedact:
    def test_removes_telegram_bot_token(self):
        text = f"429 Client Error: Too Many Requests for url: https://api.telegram.org/bot{TOKEN}/sendMessage"
        out = redact(text)
        assert TOKEN not in out
        assert "AAH-secretTokenValue_xyz" not in out
        assert "api.telegram.org" in out  # chỉ che phần bí mật, giữ ngữ cảnh để đọc lỗi

    def test_removes_url_userinfo_and_webhook_paths(self):
        out = redact(f"POST {SLACK_URL} failed")
        assert "SECRETSECRET" not in out

    def test_leaves_plain_text_alone(self):
        assert redact("SMTP timeout") == "SMTP timeout"

    @pytest.mark.parametrize("text", [
        # 3 hình dạng THẬT đo trên prod 2026-10-15 (294 dòng AlertNotification.error), token đã thay bằng giá trị giả.
        f"429 Client Error: Too Many Requests for url: https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"400 Client Error: Bad Request for url: https://api.telegram.org/bot{TOKEN}/sendMessage",
        f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: "
        f"/bot{TOKEN}/sendMessage (Caused by NameResolutionError(\"HTTPSConnection: Failed to resolve\"))",
    ])
    def test_real_production_error_shapes_are_fully_scrubbed(self, text):
        """Review 2026-10-15: bản đầu dùng `\\b` trước chữ số nên KHÔNG khớp `/bot<token>` (giữa `t` và
        chữ số không có ranh giới từ) — rò token ở 121/294 dòng thật dạng 'url: /bot<token>/sendMessage'."""
        out = redact(text)
        assert TOKEN not in out
        assert "AAH-secretTokenValue_xyz" not in out
        assert "123456789" not in out

    def test_bare_bot_path_keeps_context_but_not_the_secret(self):
        out = redact(f"Max retries exceeded with url: /bot{TOKEN}/sendMessage (Caused by Timeout)")
        assert "Max retries exceeded" in out and "(Caused by Timeout)" in out
        assert "AAH-secretTokenValue_xyz" not in out
