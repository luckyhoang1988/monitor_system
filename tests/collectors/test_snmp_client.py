"""Tests cho snmp_client — backend selection và discovery probe."""
from unittest.mock import MagicMock, patch

import pytest

from apps.collectors.snmp_client import (
    OID_SYS_DESCR,
    OID_SYS_NAME,
    probe_snmp_v2c,
    resolve_snmp_backend,
    snmp_get_value,
)


class TestResolveSnmpBackend:
    def test_returns_known_backend(self):
        backend = resolve_snmp_backend()
        assert backend in ("easysnmp", "pysnmp")


class TestProbeSnmpV2c:
    def test_probe_success(self):
        session = MagicMock()
        session.get.side_effect = [
            MagicMock(value="Cisco IOS-XE Software"),
            MagicMock(value="sw-core"),
        ]

        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="easysnmp"):
            with patch("apps.collectors.snmp_client.create_snmp_session", return_value=session):
                ok, descr = probe_snmp_v2c("192.168.1.1", "public")

        assert ok is True
        assert descr == "sw-core - Cisco IOS-XE Software"
        assert session.get.call_args_list[0][0][0] == OID_SYS_DESCR
        assert session.get.call_args_list[1][0][0] == OID_SYS_NAME

    def test_probe_no_response(self):
        session = MagicMock()
        session.get.side_effect = [MagicMock(value=""), MagicMock(value="")]

        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="pysnmp"):
            with patch("apps.collectors.snmp_client.create_snmp_session", return_value=session):
                ok, descr = probe_snmp_v2c("192.168.1.2", "public")

        assert ok is False
        assert descr == ""

    def test_probe_uses_pysnmp_backend(self):
        session = MagicMock()
        session.get.side_effect = [
            MagicMock(value="host"),
            MagicMock(value="RouterOS"),
        ]

        with patch("apps.collectors.snmp_client.resolve_snmp_backend", return_value="pysnmp") as mock_backend:
            with patch("apps.collectors.snmp_client.create_snmp_session", return_value=session) as mock_create:
                ok, descr = probe_snmp_v2c("10.0.0.1", "public")

        assert ok is True
        mock_backend.assert_called_once()
        mock_create.assert_called_once()
        kwargs = mock_create.call_args[0][0]
        assert kwargs["hostname"] == "10.0.0.1"
        assert kwargs["community"] == "public"
        assert kwargs["version"] == 2

    def test_probe_session_error_returns_false(self):
        with patch("apps.collectors.snmp_client.create_snmp_session", side_effect=ConnectionError("timeout")):
            ok, descr = probe_snmp_v2c("10.0.0.99", "public")
        assert ok is False
        assert descr == ""


class TestSnmpGetValue:
    def test_reads_easysnmp_result(self):
        session = MagicMock()
        session.get.return_value = MagicMock(value="hello")
        assert snmp_get_value(session, "1.3.6.1.2.1.1.1.0") == "hello"

    def test_returns_none_on_error(self):
        session = MagicMock()
        session.get.side_effect = ConnectionError("fail")
        assert snmp_get_value(session, "1.3.6.1.2.1.1.1.0") is None
