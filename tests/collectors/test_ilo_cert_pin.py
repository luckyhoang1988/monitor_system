"""Ghim fingerprint chứng chỉ iLO (chống giả mạo iLO khi gửi Basic Auth).

Bối cảnh (đo 3 iLO prod 2026-10-01): cert mặc định HPE, SAN không có IP -> `verify=True` fail cả 3 host, nên
hướng xác thực khả thi là ghim SHA-256. Phần "TLS thật" dùng máy chủ HTTPS local (cert tự ký sinh ngay trong
test) để chứng minh hành vi `assert_fingerprint` của urllib3 chứ không chỉ mock.
"""
import datetime
import hashlib
import select
import socket
import socketserver
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.core.management import call_command

from apps.collectors.ilo_redfish import (
    IloRedfishClient,
    _PinnedAdapter,
    fetch_cert_der,
    normalize_cert_sha256,
)
from apps.devices.forms import DeviceForm
from tests.conftest import HyperVDeviceFactory

PIN_CMD = "apps.collectors.management.commands.pin_ilo_certs"


class _TlsServer:
    """HTTPS server local, ghi lại mọi request nhận được (đường dẫn + header Authorization)."""

    def __init__(self, tmp_path):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ILOTEST")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=30))
                .sign(key, hashes.SHA256()))
        cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
        self.fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        self.requests: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("Authorization")))
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}/redfish/v1/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _ConnectProxy:
    """Proxy HTTP CONNECT local (đường hầm TCP thuần), ghi lại các đích được yêu cầu tunnel."""

    def __init__(self):
        self.connects: list[str] = []
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                method, target, _ = data.split(b"\r\n", 1)[0].decode().split(" ", 2)
                if method != "CONNECT":
                    self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                    return
                outer.connects.append(target)
                host, port = target.rsplit(":", 1)
                with socket.create_connection((host, int(port)), timeout=5) as upstream:
                    self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    peers = {self.request: upstream, upstream: self.request}
                    while True:
                        ready, _, _ = select.select(list(peers), [], [], 5)
                        if not ready:
                            return
                        for sock in ready:
                            buf = sock.recv(65536)
                            if not buf:
                                return
                            peers[sock].sendall(buf)

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def tls_server(tmp_path):
    server = _TlsServer(tmp_path)
    yield server
    server.close()


@pytest.fixture
def proxy():
    p = _ConnectProxy()
    yield p
    p.close()


def _session(adapter=None):
    session = requests.Session()
    session.verify = False
    session.auth = ("admin", "secret")
    if adapter is not None:
        session.mount("https://", adapter)
    return session


class TestPinnedAdapterOverRealTls:
    def test_matching_fingerprint_connects_and_sends_credentials(self, tls_server):
        adapter = _PinnedAdapter(tls_server.fingerprint)
        resp = _session(adapter).get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert resp.json() == {"ok": True}
        assert tls_server.requests[0][1].startswith("Basic ")
        assert adapter.fingerprint_mismatch is False

    def test_other_fingerprint_is_rejected_before_any_request_is_sent(self, tls_server):
        """Cert lạ (hoặc cert iLO bị thay) KHÔNG được nhận Basic Auth: bắt tay fail trước khi gửi request."""
        adapter = _PinnedAdapter("0" * 64)
        with pytest.raises(requests.exceptions.SSLError):
            _session(adapter).get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert adapter.fingerprint_mismatch is True
        assert tls_server.requests == []

    def test_pin_is_enforced_through_explicit_proxy(self, tls_server, proxy):
        """Review ngoài 2026-10-01: pin chỉ truyền cho PoolManager, ProxyManager bỏ qua → qua proxy sai pin
        vẫn HTTP 200 và server nhận Basic Auth. Proxy phải không cho phép lách pin."""
        session = _session(_PinnedAdapter("0" * 64))
        session.proxies = {"https": proxy.url}
        with pytest.raises(requests.exceptions.SSLError):
            session.get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert tls_server.requests == []
        assert proxy.connects, "request phải thật sự đi qua proxy (nếu không test này vô nghĩa)"

    def test_matching_pin_still_works_through_proxy(self, tls_server, proxy):
        adapter = _PinnedAdapter(tls_server.fingerprint)
        session = _session(adapter)
        session.proxies = {"https": proxy.url}
        resp = session.get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert resp.json() == {"ok": True}
        assert proxy.connects and adapter.fingerprint_mismatch is False

    def test_pin_is_enforced_when_proxy_comes_from_environment(self, tls_server, proxy, monkeypatch):
        """Requests mặc định đọc HTTPS_PROXY từ môi trường (trust_env) — đúng kịch bản trong báo cáo."""
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("https_proxy", proxy.url)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        session = requests.Session()
        assert session.trust_env is True
        session.verify = False
        session.auth = ("admin", "secret")
        session.mount("https://", _PinnedAdapter("0" * 64))
        with pytest.raises(requests.exceptions.SSLError):
            session.get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert tls_server.requests == []
        assert proxy.connects

    @pytest.mark.filterwarnings("ignore::urllib3.exceptions.InsecureRequestWarning")
    def test_unpinned_session_would_send_credentials_to_any_cert(self, tls_server):
        """Đối chứng: không ghim thì verify=False gửi credentials cho cert nào cũng được (rủi ro P1)."""
        _session().get(tls_server.url, timeout=5, headers={"Connection": "close"})
        assert tls_server.requests[0][1].startswith("Basic ")

    def test_fetch_cert_der_returns_presented_certificate(self, tls_server):
        der = fetch_cert_der("127.0.0.1", port=tls_server.port, timeout=5)
        assert hashlib.sha256(der).hexdigest() == tls_server.fingerprint


@pytest.mark.parametrize("value,expected", [
    ("AB" * 32, "ab" * 32),
    (":".join(["AB"] * 32), "ab" * 32),
    (" " + "ab-" * 31 + "ab ", "ab" * 32),
    ("", None),
    (None, None),
    ("ab" * 31, None),            # thiếu 1 byte
    ("zz" * 32, None),            # không phải hex
    ("ab" * 33, None),            # thừa
])
def test_normalize_cert_sha256(value, expected):
    assert normalize_cert_sha256(value) == expected


@pytest.mark.django_db
class TestCollectRawWithPin:
    def test_invalid_pin_fails_closed_without_touching_network(self, mocker):
        """Pin nhập sai định dạng không được lặng lẽ rơi về 'không xác thực' (tưởng đã được bảo vệ)."""
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="not-a-fingerprint")
        get = mocker.patch("requests.Session.get")
        client = IloRedfishClient(host)
        assert client.collect_raw() is None
        assert "sai định dạng" in client.last_error
        get.assert_not_called()

    def test_fingerprint_mismatch_reports_clear_reason(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="a" * 64)
        mocker.patch("requests.adapters.HTTPAdapter.send",
                     side_effect=requests.exceptions.SSLError("Fingerprints did not match"))
        client = IloRedfishClient(host)
        assert client.collect_raw() is None
        assert "KHÔNG khớp fingerprint" in client.last_error
        assert len(client.last_error) <= 200  # vừa cột Device.ilo_last_error

    def test_pinned_session_ignores_proxy_environment(self, mocker):
        """Session đã ghim không đọc proxy/netrc/CA từ môi trường; session chưa ghim giữ hành vi cũ."""
        sessions = []

        def fake_get(session, *args, **kwargs):
            sessions.append(session)
            resp = mocker.MagicMock(status_code=401)
            resp.json.return_value = {}
            return resp

        mocker.patch("requests.Session.get", autospec=True, side_effect=fake_get)
        IloRedfishClient(HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="ab" * 32)).collect_raw()
        IloRedfishClient(HyperVDeviceFactory(ilo_ip_address="10.1.1.2")).collect_raw()
        pinned_session, unpinned_session = sessions
        assert pinned_session.trust_env is False
        assert unpinned_session.trust_env is True

    def test_plain_ssl_error_without_pin_keeps_generic_message(self, mocker):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        mocker.patch("requests.adapters.HTTPAdapter.send",
                     side_effect=requests.exceptions.SSLError("boom"))
        client = IloRedfishClient(host)
        assert client.collect_raw() is None
        assert "fingerprint" not in client.last_error

    def test_pin_mounts_pinned_adapter_and_disables_chain_check(self, mocker):
        # Cột lưu 64 ký tự (form/lệnh luôn chuẩn hoá trước); chữ HOA vẫn phải được chấp nhận và hạ về thường.
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="AB" * 32)
        mount = mocker.spy(requests.Session, "mount")
        resp = mocker.MagicMock(status_code=401)
        resp.json.return_value = {}
        mocker.patch("requests.Session.get", return_value=resp)
        IloRedfishClient(host).collect_raw()
        prefix, adapter = mount.call_args.args[-2:]  # args = (session, prefix, adapter) vì spy trên method của class
        assert prefix == "https://" and isinstance(adapter, _PinnedAdapter)
        assert adapter._fingerprint == "ab" * 32


class TestDeviceFormPinField:
    def _clean(self, value):
        form = DeviceForm()
        form.cleaned_data = {"ilo_cert_sha256": value}
        return form.clean_ilo_cert_sha256()

    def test_colon_separated_input_is_normalized(self):
        assert self._clean(":".join(["AB"] * 32)) == "ab" * 32

    def test_blank_is_allowed(self):
        assert self._clean("") == ""

    def test_garbage_is_rejected(self):
        from django import forms
        with pytest.raises(forms.ValidationError):
            self._clean("abc")

    def test_pasted_colon_form_fits_the_field_length(self):
        field = DeviceForm().fields["ilo_cert_sha256"]
        assert len(":".join(["AB"] * 32)) == 95 <= field.max_length


@pytest.mark.django_db
class TestPinCommand:
    @pytest.fixture(autouse=True)
    def _fake_cert(self, mocker):
        self.der = b"fake-der-bytes"
        self.fingerprint = hashlib.sha256(self.der).hexdigest()
        self.fetch = mocker.patch(f"{PIN_CMD}.fetch_cert_der", return_value=self.der)
        mocker.patch(f"{PIN_CMD}._describe", return_value="subject=CN=ILOTEST")

    def _run(self, *args):
        out = StringIO()
        call_command("pin_ilo_certs", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_prints_fingerprint_and_writes_nothing(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        out = self._run()
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == ""
        assert self.fingerprint[:2].upper() in out and "chưa ghim" in out and "chưa ghi gì" in out

    def test_apply_pins_unpinned_host(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        self._run("--apply")
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == self.fingerprint

    def test_changed_cert_not_overwritten_without_replace(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="a" * 64)
        out = self._run("--apply")
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == "a" * 64
        assert "KHÁC cert hiện tại" in out

    def test_replace_overwrites_changed_cert(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256="a" * 64)
        self._run("--apply", "--replace")
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == self.fingerprint

    def test_matching_pin_is_left_alone(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1", ilo_cert_sha256=self.fingerprint)
        out = self._run("--apply")
        assert "KHỚP" in out
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == self.fingerprint

    def test_unreachable_host_is_reported_not_fatal_and_not_pinned(self):
        host = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        self.fetch.side_effect = OSError("timed out")
        out = self._run("--apply")
        host.refresh_from_db()
        assert host.ilo_cert_sha256 == ""
        assert "không đọc được chứng chỉ" in out

    def test_device_filter(self):
        a = HyperVDeviceFactory(ilo_ip_address="10.1.1.1")
        b = HyperVDeviceFactory(ilo_ip_address="10.1.1.2")
        self._run("--apply", "--device", str(a.pk))
        a.refresh_from_db(); b.refresh_from_db()
        assert a.ilo_cert_sha256 == self.fingerprint and b.ilo_cert_sha256 == ""
