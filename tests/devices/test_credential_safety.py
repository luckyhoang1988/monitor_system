"""1.6 — giải mã credential an toàn: token không giải mã được KHÔNG được trả về như mật khẩu.

Bối cảnh (A6 trong kế hoạch review 2026-10-15): `decrypt_value` nuốt mọi lỗi rồi trả chính ciphertext.
Thiếu/đổi `ENCRYPTION_KEY` → collector dùng chuỗi `gAAAA...` làm mật khẩu WinRM/iLO/SSH (mỗi lần poll là
một lần đăng nhập sai → khoá tài khoản AD/AAA/iLO), và bất kỳ `device.save()` đầy đủ nào ghi đè cột bằng
`encrypt(ciphertext)` — mất credential vĩnh viễn kể cả khi sau đó đặt lại đúng khoá.

Prod 2026-10-15 chưa gặp sự cố này (23 thiết bị, mọi token giải mã được): đây là phòng thủ, không phải sửa
sự cố đã quan sát.
"""
from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection

from apps.devices import crypto
from apps.devices.crypto import _get_fernet, decrypt_value, encrypt_value, is_encrypted
from apps.devices.models import Device

KEY_A = "key-A-for-credential-safety-tests-0001"
KEY_B = "key-B-for-credential-safety-tests-0002"

SECRET_COMMUNITY = "community-secret-123"
SECRET_SSH = "ssh-secret-456"


@pytest.fixture(autouse=True)
def _fernet_cache(settings):
    _get_fernet.cache_clear()
    settings.ENCRYPTION_KEY = KEY_A
    yield
    _get_fernet.cache_clear()


def use_key(settings, key: str) -> None:
    settings.ENCRYPTION_KEY = key
    _get_fernet.cache_clear()


def raw_columns(device_id: int) -> tuple[str, str]:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT snmp_community, ssh_password FROM devices_device WHERE id = %s", [device_id],
        )
        return cur.fetchone()


def make_device(**extra) -> Device:
    defaults = dict(
        name="cred-safety-1", device_type="switch", ip_address="10.9.9.1", vendor="cisco",
        protocol="ssh", snmp_community=SECRET_COMMUNITY, ssh_username="admin", ssh_password=SECRET_SSH,
    )
    defaults.update(extra)
    return Device.objects.create(**defaults)


class TestDecryptValue:
    def test_token_under_another_key_raises_instead_of_returning_ciphertext(self, settings):
        token = encrypt_value("p@ss")
        use_key(settings, KEY_B)
        with pytest.raises(crypto.CredentialDecryptError):
            decrypt_value(token)

    def test_tampered_token_raises(self):
        token = encrypt_value("p@ss")
        broken = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
        with pytest.raises(crypto.CredentialDecryptError):
            decrypt_value(broken)

    def test_missing_key_for_a_token_raises_decrypt_error(self, settings):
        token = encrypt_value("p@ss")
        use_key(settings, "")
        with pytest.raises(crypto.CredentialDecryptError) as info:
            decrypt_value(token)
        assert "ENCRYPTION_KEY" in str(info.value)

    def test_encrypting_without_key_still_fails_loudly(self, settings):
        use_key(settings, "")
        with pytest.raises(RuntimeError):
            encrypt_value("p@ss")

    def test_legacy_plaintext_is_returned_as_is_even_without_key(self, settings):
        use_key(settings, "")
        assert decrypt_value("public") == "public"

    @pytest.mark.parametrize("value", ["public", "P@ssw0rd!", "gAmer-2024", "gAAAA-four-As", "dGVzdA==", "a" * 120])
    def test_plaintext_that_merely_resembles_a_token_is_legacy_plaintext(self, value):
        # Không có phần đầu của Fernet token (`gAAAAA`) → không có bằng chứng là token → plain-text cũ.
        assert decrypt_value(value) == value

    def test_error_message_never_contains_the_stored_value(self, settings):
        token = encrypt_value("p@ss")
        use_key(settings, KEY_B)
        with pytest.raises(crypto.CredentialDecryptError) as info:
            decrypt_value(token)
        assert token not in str(info.value)


class TestLooksEncrypted:
    def test_token_under_current_key(self):
        assert crypto.looks_encrypted(encrypt_value("x")) is True

    def test_token_under_another_key_still_looks_encrypted_but_is_not_decryptable(self, settings):
        token = encrypt_value("x")
        use_key(settings, KEY_B)
        assert crypto.looks_encrypted(token) is True
        assert is_encrypted(token) is False

    @pytest.mark.parametrize("value", ["", "public", "gAAAAA", "gAAAAA-not-a-real-token!", "a" * 120])
    def test_plain_values_do_not_look_encrypted(self, value):
        assert crypto.looks_encrypted(value) is False


class TestDamagedTokenIsNotLegacyPlaintext:
    """Review ngoài (P2) sau 1.6: chuỗi KHÔNG đạt kiểm tra cấu trúc không mặc nhiên là plain-text cũ.

    Token mất padding `=` hoặc bị cắt cụt không giải mã được nhưng cũng không phải mật khẩu. Nếu coi là
    plain-text thì collector đăng nhập bằng chính chuỗi `gAAAAA…` và `save()` mã hoá chồng lên nó.
    """

    # Token dài 73 byte (plaintext ≤15 byte) kết thúc bằng "==", 89 byte (16–31 byte) bằng "=".
    @pytest.mark.parametrize("plaintext,padding", [("x", "=="), ("sixteen-chars-ok", "=")])
    def test_token_that_lost_its_padding_is_refused(self, plaintext, padding):
        token = encrypt_value(plaintext)
        assert token.endswith(padding) and not token.endswith(padding + "=")      # tiền đề của test
        damaged = token.rstrip("=")

        assert crypto.looks_encrypted(damaged) is False     # đúng như báo cáo: không có cấu trúc hợp lệ
        assert crypto.looks_damaged_token(damaged) is True
        with pytest.raises(crypto.CredentialDecryptError) as info:
            decrypt_value(damaged)
        assert damaged not in str(info.value)

    @pytest.mark.parametrize("keep", [8, 30, 60, 90, 99])
    def test_truncated_token_is_refused(self, keep):
        damaged = encrypt_value("x")[:keep]

        assert crypto.looks_damaged_token(damaged) is True
        with pytest.raises(crypto.CredentialDecryptError):
            decrypt_value(damaged)

    def test_token_with_stray_whitespace_is_refused(self):
        token = encrypt_value("x")
        for damaged in (" " + token, "\t" + token, token[:40] + " " + token[40:]):
            with pytest.raises(crypto.CredentialDecryptError):
                decrypt_value(damaged)

    def test_damaged_token_is_refused_even_without_a_key(self, settings):
        damaged = encrypt_value("x").rstrip("=")
        use_key(settings, "")
        with pytest.raises(crypto.CredentialDecryptError):
            decrypt_value(damaged)

    def test_reason_tells_the_operator_the_value_is_damaged_not_that_the_key_is_wrong(self):
        with pytest.raises(crypto.CredentialDecryptError) as info:
            decrypt_value(encrypt_value("x").rstrip("="))
        assert "hỏng" in str(info.value) and "ENCRYPTION_KEY" not in str(info.value)

    @pytest.mark.parametrize("value", ["", "public", "P@ssw0rd!", "gAmer-2024", "gAAAA-four-As", "dGVzdA==",
                                        "a" * 120])
    def test_ordinary_values_and_valid_tokens_are_not_damaged(self, value):
        assert crypto.looks_damaged_token(value) is False

    def test_valid_token_is_not_damaged(self, settings):
        token = encrypt_value("x")
        assert crypto.looks_damaged_token(token) is False
        use_key(settings, KEY_B)
        assert crypto.looks_damaged_token(token) is False    # token của khoá khác: lỗi khoá, không phải hỏng


@pytest.mark.django_db
class TestFieldWithWrongKey:
    def test_device_still_loads_and_exposes_unreadable_credentials(self, settings):
        device = make_device()
        use_key(settings, KEY_B)

        loaded = Device.objects.get(pk=device.pk)   # KHÔNG được ném: 1 credential hỏng không được hạ cả fleet

        assert loaded.name == "cred-safety-1"
        for value in (loaded.snmp_community, loaded.ssh_password):
            assert isinstance(value, crypto.UnreadableCredential)
            assert value == ""          # falsy: mọi `if not password` hiện có đều từ chối thay vì đăng nhập
            assert not value
            assert value.reason

    def test_queryset_with_one_unreadable_row_does_not_break_the_others(self, settings):
        good = make_device(name="good", ip_address="10.9.9.2", ssh_password="")
        bad = make_device(name="bad", ip_address="10.9.9.3")
        # Hỏng đúng 1 dòng: ghi token của khoá khác vào cột ssh_password.
        other = _token_under(settings, KEY_B, "whatever")
        with connection.cursor() as cur:
            cur.execute("UPDATE devices_device SET ssh_password = %s WHERE id = %s", [other, bad.pk])

        loaded = {d.name: d for d in Device.objects.filter(pk__in=[good.pk, bad.pk])}

        assert loaded["good"].snmp_community == SECRET_COMMUNITY
        assert loaded["bad"].snmp_community == SECRET_COMMUNITY
        assert isinstance(loaded["bad"].ssh_password, crypto.UnreadableCredential)

    def test_full_save_keeps_the_stored_token_byte_for_byte(self, settings):
        device = make_device()
        before = raw_columns(device.pk)
        use_key(settings, KEY_B)

        loaded = Device.objects.get(pk=device.pk)
        loaded.notes = "đổi ghi chú, không đụng credential"
        loaded.save()          # save ĐẦY ĐỦ — đúng đường mà form/admin dùng

        assert raw_columns(device.pk) == before

    def test_restoring_the_right_key_after_a_save_still_reads_the_credentials(self, settings):
        device = make_device()
        use_key(settings, KEY_B)
        loaded = Device.objects.get(pk=device.pk)
        loaded.save()

        use_key(settings, KEY_A)
        restored = Device.objects.get(pk=device.pk)

        assert restored.snmp_community == SECRET_COMMUNITY
        assert restored.ssh_password == SECRET_SSH

    def test_entering_a_new_value_replaces_the_unreadable_one(self, settings):
        device = make_device()
        use_key(settings, KEY_B)
        loaded = Device.objects.get(pk=device.pk)

        loaded.ssh_password = "mật-khẩu-mới"
        loaded.save()

        use_key(settings, KEY_B)
        again = Device.objects.get(pk=device.pk)
        assert again.ssh_password == "mật-khẩu-mới"

    def test_blank_form_value_does_not_wipe_the_unreadable_credential(self, settings):
        device = make_device()
        before = raw_columns(device.pk)
        use_key(settings, KEY_B)
        loaded = Device.objects.get(pk=device.pk)

        field = Device._meta.get_field("ssh_password")
        field.save_form_data(loaded, "")       # đúng thứ ModelForm gọi khi ô mật khẩu để trống
        loaded.save()

        assert raw_columns(device.pk) == before

    def test_form_value_replaces_unreadable_credential(self, settings):
        device = make_device()
        use_key(settings, KEY_B)
        loaded = Device.objects.get(pk=device.pk)

        field = Device._meta.get_field("ssh_password")
        field.save_form_data(loaded, "new-one")
        loaded.save()

        assert Device.objects.get(pk=device.pk).ssh_password == "new-one"

    def test_edit_form_warns_when_a_stored_credential_is_unreadable(self, settings):
        from apps.devices.forms import DeviceForm

        device = make_device()
        use_key(settings, KEY_B)
        loaded = Device.objects.get(pk=device.pk)

        form = DeviceForm(instance=loaded)

        assert set(form.unreadable_credentials) == {"snmp_community", "ssh_password"}
        assert DeviceForm().unreadable_credentials == {}


def _token_under(settings, key: str, plaintext: str) -> str:
    previous = settings.ENCRYPTION_KEY
    use_key(settings, key)
    try:
        return encrypt_value(plaintext)
    finally:
        use_key(settings, previous)


@pytest.mark.django_db
class TestEncryptCredentialsCommand:
    def test_does_not_double_encrypt_a_token_it_cannot_read(self, settings):
        device = make_device()
        before = raw_columns(device.pk)
        use_key(settings, KEY_B)

        call_command("encrypt_credentials")

        assert raw_columns(device.pk) == before

    def test_still_encrypts_real_plaintext(self, settings):
        device = make_device(snmp_community="", ssh_password="")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE devices_device SET snmp_community = 'public', ssh_password = 'pw' WHERE id = %s",
                [device.pk],
            )

        call_command("encrypt_credentials")

        community, password = raw_columns(device.pk)
        assert community != "public" and password != "pw"
        loaded = Device.objects.get(pk=device.pk)
        assert (loaded.snmp_community, loaded.ssh_password) == ("public", "pw")


@pytest.mark.django_db
class TestDamagedTokenInDatabase:
    """Cùng kịch bản `TestFieldWithWrongKey` nhưng cột bị cắt/mất padding thay vì khoá sai."""

    def _damaged_device(self) -> tuple[Device, str, str]:
        device = make_device()
        community = encrypt_value(SECRET_COMMUNITY).rstrip("=")
        password = encrypt_value(SECRET_SSH).rstrip("=")
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE devices_device SET snmp_community = %s, ssh_password = %s WHERE id = %s",
                [community, password, device.pk],
            )
        return Device.objects.get(pk=device.pk), community, password

    def test_value_is_unreadable_not_a_usable_password(self):
        loaded, _, _ = self._damaged_device()

        for value in (loaded.snmp_community, loaded.ssh_password):
            assert isinstance(value, crypto.UnreadableCredential)
            assert value == ""
        with pytest.raises(crypto.CredentialDecryptError):
            crypto.ensure_credentials_readable(loaded, "snmp_community", "ssh_password")

    def test_full_save_does_not_encrypt_over_the_damaged_value(self):
        loaded, community, password = self._damaged_device()

        loaded.notes = "đổi ghi chú, không đụng credential"
        loaded.save()

        assert raw_columns(loaded.pk) == (community, password)

    def test_ssh_collector_never_logs_in_with_the_damaged_token(self, mocker):
        from apps.collectors.switch_ssh import SwitchSSHCollector

        loaded, _, _ = self._damaged_device()
        connect = mocker.patch("netmiko.ConnectHandler")

        with pytest.raises(crypto.CredentialDecryptError):
            SwitchSSHCollector(loaded)._get_connection()

        connect.assert_not_called()

    def test_error_and_log_do_not_contain_the_damaged_value(self, caplog):
        from apps.devices import fields as fields_module

        fields_module._warned.clear()
        with caplog.at_level("DEBUG"):
            loaded, community, password = self._damaged_device()
        with pytest.raises(crypto.CredentialDecryptError) as info:
            crypto.ensure_credentials_readable(loaded, "snmp_community", "ssh_password")

        for value in (community, password):
            assert value not in str(info.value)
            assert value not in caplog.text

    def test_encrypt_credentials_does_not_encrypt_over_or_hide_the_damaged_value(self):
        from io import StringIO

        loaded, community, password = self._damaged_device()
        out = StringIO()

        call_command("encrypt_credentials", stdout=out)

        assert raw_columns(loaded.pk) == (community, password)
        report = out.getvalue()
        assert f"Device #{loaded.pk}: encrypted" not in report
        assert "snmp_community" in report and "ssh_password" in report      # báo riêng, không im lặng
        assert community not in report and password not in report


def _unreadable(reason: str = "key sai") -> crypto.UnreadableCredential:
    return crypto.UnreadableCredential("gAAAAAraw", reason)


class TestUnreadableCredentialValue:
    def test_is_falsy_empty_string_carrying_raw_and_reason(self):
        value = _unreadable("lý do")
        assert value == "" and not value
        assert value.raw == "gAAAAAraw"
        assert value.reason == "lý do"
        assert isinstance(value, str)

    def test_survives_copy_and_deepcopy(self):
        import copy
        value = _unreadable("lý do")
        for clone in (copy.copy(value), copy.deepcopy(value)):
            assert isinstance(clone, crypto.UnreadableCredential)
            assert clone.raw == "gAAAAAraw" and clone.reason == "lý do"

    def test_strip_and_slicing_do_not_crash_callers(self):
        value = _unreadable()
        assert value.strip() == ""
        assert value[:3] == ""


class TestEnsureCredentialsReadable:
    class _Dev:
        name = "SW-X"

        def __init__(self, **attrs):
            self.__dict__.update(attrs)

    def test_passes_for_readable_values(self):
        device = self._Dev(ssh_password="pw", snmp_community="")
        crypto.ensure_credentials_readable(device, "ssh_password", "snmp_community")

    def test_raises_naming_device_and_fields_but_not_secrets(self):
        device = self._Dev(ssh_password=_unreadable("ENCRYPTION_KEY sai"), snmp_community="ok")
        with pytest.raises(crypto.CredentialDecryptError) as info:
            crypto.ensure_credentials_readable(device, "ssh_password", "snmp_community")
        message = str(info.value)
        assert "SW-X" in message and "ssh_password" in message and "ENCRYPTION_KEY sai" in message
        assert "snmp_community" not in message
        assert "gAAAAAraw" not in message

    def test_missing_attribute_is_ignored(self):
        crypto.ensure_credentials_readable(self._Dev(), "ssh_password")


@pytest.mark.django_db
class TestConnectorsRefuseUnreadableCredentials:
    """Mỗi đường đăng nhập phải từ chối TRƯỚC khi gửi bất cứ gói nào có credential."""

    def _device(self, settings, **extra) -> Device:
        device = make_device(**extra)
        use_key(settings, KEY_B)
        return Device.objects.get(pk=device.pk)

    def test_neither_reason_nor_error_nor_log_contains_the_stored_token(self, settings, caplog):
        from apps.devices import fields as fields_module

        device = make_device()
        stored_community, stored_password = raw_columns(device.pk)
        use_key(settings, KEY_B)
        fields_module._warned.clear()      # để dòng log cảnh báo thật sự được phát trong test này

        with caplog.at_level("DEBUG"):
            loaded = Device.objects.get(pk=device.pk)
        with pytest.raises(crypto.CredentialDecryptError) as info:
            crypto.ensure_credentials_readable(loaded, "snmp_community", "ssh_password")

        for token in (stored_community, stored_password):
            assert token not in str(info.value)
            assert token not in loaded.ssh_password.reason
            assert token not in caplog.text
        assert "snmp_community" in caplog.text    # có log, và chỉ nêu tên trường + nguyên nhân

    def test_snmp_collector_v2c(self, settings, mocker):
        from apps.collectors.switch_snmp import SwitchSNMPCollector

        device = self._device(settings, protocol="snmp", snmp_version="v2c")
        factory = mocker.patch("apps.collectors.switch_snmp.create_snmp_session")

        collector = SwitchSNMPCollector(device)   # tạo collector KHÔNG được ném (factory gọi ngoài try)
        with pytest.raises(crypto.CredentialDecryptError):
            collector.collect()

        factory.assert_not_called()

    def test_snmp_collector_v3_does_not_downgrade_to_no_auth(self, settings, mocker):
        from apps.collectors.switch_snmp import SwitchSNMPCollector

        device = make_device(
            protocol="snmp", snmp_version="v3", snmp_community="", snmpv3_username="u",
            snmpv3_auth_protocol="SHA", snmpv3_auth_password="authpass-1234",
        )
        use_key(settings, KEY_B)
        device = Device.objects.get(pk=device.pk)
        factory = mocker.patch("apps.collectors.switch_snmp.create_snmp_session")

        collector = SwitchSNMPCollector(device)
        with pytest.raises(crypto.CredentialDecryptError):
            collector.collect()

        factory.assert_not_called()   # trước đây dựng phiên no_auth_or_privacy rồi chờ timeout

    def test_poll_goes_offline_with_reason_instead_of_crashing(self, settings, mocker, caplog):
        """`_poll_device_once` tạo collector NGOÀI try: lỗi credential phải đi đường "Poll collect failed"."""
        import logging

        from django.utils import timezone

        from apps.collectors.tasks import poll_device

        device = make_device(
            protocol="snmp", snmp_version="v2c", last_seen=timezone.now(), last_ok_seen=timezone.now(),
        )
        use_key(settings, KEY_B)
        mocker.patch("apps.collectors.ping_util.icmp_ping", return_value=(True, 1.0))
        publish = mocker.patch("apps.realtime.publisher.publish_device_event")
        factory = mocker.patch("apps.collectors.switch_snmp.create_snmp_session")

        with caplog.at_level(logging.WARNING, logger="apps.collectors.tasks"):
            poll_device.run(device.pk)      # không được ném

        factory.assert_not_called()
        assert Device.objects.get(pk=device.pk).last_seen is None
        online = publish.call_args.kwargs.get("online", publish.call_args.args[1])
        assert online is False
        messages = [r.getMessage() for r in caplog.records if "Poll collect failed" in r.getMessage()]
        assert messages and "không giải mã" in messages[0] and "ENCRYPTION_KEY" in messages[0]

    def test_snmp_session_factory_is_a_choke_point_for_topology_walks(self, settings):
        from apps.collectors.snmp_client import create_snmp_session

        with pytest.raises(crypto.CredentialDecryptError):
            create_snmp_session(
                {"hostname": "192.0.2.1", "version": 2, "community": _unreadable(),
                 "timeout": 1, "retries": 0},
                backend="pysnmp",
            )

    def test_hyperv_winrm_never_opens_a_session(self, settings, mocker):
        from apps.collectors.hyperv import HyperVCollector

        device = self._device(settings, device_type="hyperv", protocol="winrm")
        session = mocker.patch("winrm.Session")

        with pytest.raises(crypto.CredentialDecryptError):
            HyperVCollector(device)._run_ps("$env:COMPUTERNAME")

        session.assert_not_called()

    def test_ilo_poll_reports_reason_and_sends_nothing(self, settings, mocker):
        from apps.collectors.ilo_redfish import IloRedfishClient

        device = make_device(
            device_type="hyperv", protocol="winrm", ilo_ip_address="10.9.9.50", ilo_username="admin",
            ilo_password="ilo-secret-789",
        )
        use_key(settings, KEY_B)
        device = Device.objects.get(pk=device.pk)
        client = IloRedfishClient(device)
        get = mocker.patch.object(client, "_get")

        assert client.collect_raw() is None

        assert "giải mã" in (client.last_error or "")
        get.assert_not_called()

    def test_ssh_collector_never_calls_netmiko(self, settings, mocker):
        from apps.collectors.switch_ssh import SwitchSSHCollector

        device = self._device(settings)
        connect = mocker.patch("netmiko.ConnectHandler")

        with pytest.raises(crypto.CredentialDecryptError):
            SwitchSSHCollector(device)._get_connection()

        connect.assert_not_called()

    def test_legacy_backup_never_calls_netmiko(self, settings, mocker):
        from apps.devices import backup

        device = self._device(settings, os_family="cisco_ios")
        connect = mocker.patch.object(backup, "ConnectHandler")

        with pytest.raises(crypto.CredentialDecryptError):
            backup.run_ssh_backup(device)

        connect.assert_not_called()
