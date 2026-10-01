"""Đọc fingerprint SHA-256 chứng chỉ của từng iLO và (với --apply) ghim vào Device.ilo_cert_sha256.

Vì sao cần: collector gọi iLO bằng Basic Auth, mà cả 3 iLO prod dùng chứng chỉ mặc định HPE (issuer
"Default Issuer (Do not trust)", SAN chỉ có tên iLO, không có IP) nên `verify=True` fail — chỉ có thể
ghim đúng chứng chỉ đó (xem `_PinnedAdapter` trong apps/collectors/ilo_redfish.py).

⚠️ Đây là ghim-lần-đầu (TOFU): lệnh tin chứng chỉ iLO đang trình ra TẠI THỜI ĐIỂM chạy. Chạy từ monitorsrv
(mạng quản trị tin cậy) và nên đối chiếu fingerprint in ra qua một nguồn ĐỘC LẬP trước khi --apply, vd mở
https://<ip iLO> bằng trình duyệt trên máy quản trị khác rồi xem chứng chỉ (SHA-256 thumbprint). Chưa kiểm
chứng giao diện web iLO có tự hiển thị SHA-256 hay không — đừng giả định. Mặc định CHỈ IN, không ghi DB.

    python manage.py pin_ilo_certs                      # xem fingerprint + trạng thái ghim của mọi host
    python manage.py pin_ilo_certs --apply              # ghim host CHƯA ghim
    python manage.py pin_ilo_certs --device 19 --apply --replace   # cert iLO đã đổi hợp lệ -> ghim lại
"""
from __future__ import annotations

import hashlib

from cryptography import x509
from django.core.management.base import BaseCommand

from apps.collectors.ilo_redfish import fetch_cert_der, normalize_cert_sha256
from apps.devices.models import Device


def _pretty(fingerprint: str) -> str:
    return ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)).upper()


def _describe(der: bytes) -> str:
    cert = x509.load_der_x509_certificate(der)
    return f"subject={cert.subject.rfc4514_string()} | issuer={cert.issuer.rfc4514_string()} | hết hạn {cert.not_valid_after_utc:%Y-%m-%d}"


class Command(BaseCommand):
    help = "Đọc SHA-256 chứng chỉ iLO và (với --apply) ghim vào Device.ilo_cert_sha256"

    def add_arguments(self, parser) -> None:
        parser.add_argument("--device", type=int, action="append", dest="devices",
                            help="id thiết bị (lặp lại được); mặc định mọi host có IP iLO")
        parser.add_argument("--apply", action="store_true",
                            help="Ghi fingerprint cho host CHƯA ghim (mặc định chỉ in, không ghi DB)")
        parser.add_argument("--replace", action="store_true",
                            help="Kèm --apply: cho ghi đè pin cũ khi cert hiện tại KHÁC pin đã lưu")

    def handle(self, *args, **options) -> None:
        qs = Device.objects.exclude(ilo_ip_address__isnull=True).order_by("pk")
        if options["devices"]:
            qs = qs.filter(pk__in=options["devices"])

        applied = pending = problems = 0
        for device in qs:
            ip = str(device.ilo_ip_address)
            try:
                der = fetch_cert_der(ip)
            except OSError as exc:
                problems += 1
                self.stdout.write(self.style.ERROR(f"[{device.pk}] {device.name} ({ip}): không đọc được chứng chỉ — {exc}"))
                continue

            fingerprint = hashlib.sha256(der).hexdigest()
            self.stdout.write(f"[{device.pk}] {device.name} ({ip})")
            self.stdout.write(f"    {_describe(der)}")
            self.stdout.write(f"    SHA-256 = {_pretty(fingerprint)}")

            stored = normalize_cert_sha256(device.ilo_cert_sha256)
            if device.ilo_cert_sha256 and stored is None:
                self.stdout.write(self.style.WARNING("    pin đang lưu SAI định dạng — coi như chưa ghim"))
            if stored == fingerprint:
                self.stdout.write(self.style.SUCCESS("    đã ghim — KHỚP cert hiện tại"))
                continue
            if stored is not None:
                self.stdout.write(self.style.WARNING(
                    f"    đã ghim nhưng KHÁC cert hiện tại (pin cũ {_pretty(stored)}) — cert iLO đã đổi hoặc bị thay"))
                if not (options["apply"] and options["replace"]):
                    pending += 1
                    continue
            elif not options["apply"]:
                self.stdout.write("    chưa ghim")
                pending += 1
                continue

            device.ilo_cert_sha256 = fingerprint
            device.save(update_fields=["ilo_cert_sha256"])
            applied += 1
            self.stdout.write(self.style.SUCCESS("    ĐÃ ghim"))

        summary = f"Xong: ghim {applied} host, còn {pending} host chưa ghim/cần --replace, {problems} host lỗi kết nối."
        if not options["apply"]:
            summary += " (chạy thử — chưa ghi gì; thêm --apply để ghim)"
        self.stdout.write(self.style.SUCCESS(summary))
