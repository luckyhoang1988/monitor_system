"""Dò và xác minh OID WLAN trên Huawei AC/ACL qua SNMP.

Dùng để chốt đúng cột bảng AP/STA trước khi tin số liệu của collector
(các cột .x khác nhau theo phiên bản VRP/AC).

Usage:
    python manage.py verify_wlan_oids <device_id>
    python manage.py verify_wlan_oids --ip 10.0.198.199 --community public
    python manage.py verify_wlan_oids 5 --limit 30
"""
from pathlib import Path

import yaml
from django.core.management.base import BaseCommand, CommandError

OID_DIR = Path(__file__).resolve().parents[4] / "oids"
WLAN_PARENT_DEFAULT = "1.3.6.1.4.1.2011.6.139"


class Command(BaseCommand):
    help = "Walk subtree WLAN của Huawei AC để xác minh OID AP/STA."

    def add_arguments(self, parser):
        parser.add_argument("device_id", nargs="?", type=int,
                            help="ID của Device (WLAN controller) đã lưu trong DB.")
        parser.add_argument("--ip", help="IP thiết bị (nếu không dùng device_id).")
        parser.add_argument("--community", default="public",
                            help="SNMP community (chỉ dùng khi truyền --ip).")
        parser.add_argument("--version", type=int, default=2, choices=[1, 2],
                            help="SNMP version khi dùng --ip (1 hoặc 2c).")
        parser.add_argument("--parent", default=None,
                            help="OID parent để walk (mặc định lấy từ profile wlan).")
        parser.add_argument("--limit", type=int, default=40,
                            help="Số dòng tối đa in cho mỗi cột (tránh tràn màn hình).")

    def handle(self, *args, **opts):
        from apps.collectors.snmp_client import (
            create_snmp_session,
            resolve_snmp_backend,
            snmp_walk_pairs,
        )

        device_id = opts.get("device_id")
        ip = opts.get("ip")

        if device_id:
            from apps.devices.models import Device
            try:
                device = Device.objects.get(pk=device_id)
            except Device.DoesNotExist as exc:
                raise CommandError(f"Device id={device_id} không tồn tại.") from exc
            version_map = {"v1": 1, "v2c": 2}
            version = version_map.get(device.snmp_version, 2)
            snmp_kwargs = {
                "hostname": device.ip_address,
                "version": version,
                "community": device.snmp_community,
                "timeout": 10,
                "retries": 2,
            }
            target = f"{device.name} ({device.ip_address})"
        elif ip:
            snmp_kwargs = {
                "hostname": ip,
                "version": opts["version"],
                "community": opts["community"],
                "timeout": 10,
                "retries": 2,
            }
            target = ip
        else:
            raise CommandError("Cần truyền device_id hoặc --ip.")

        parent = opts.get("parent")
        if not parent:
            profile_path = OID_DIR / "huawei_vrp.yaml"
            if profile_path.exists():
                profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
                parent = ((profile.get("wlan") or {}).get("enterprise_parent")) or WLAN_PARENT_DEFAULT
            else:
                parent = WLAN_PARENT_DEFAULT

        limit = opts["limit"]
        backend = resolve_snmp_backend()
        self.stdout.write(self.style.NOTICE(
            f"SNMP backend={backend} | target={target} | walk parent={parent}"
        ))

        session = create_snmp_session(snmp_kwargs, backend=backend)
        rows = snmp_walk_pairs(session, parent)
        if not rows:
            self.stdout.write(self.style.ERROR(
                "Không nhận được dữ liệu. Kiểm tra: community/version, SNMP view có mở "
                f"subtree {parent}, ACL SNMP cho IP server giám sát."
            ))
            return

        # Gom theo cột (prefix = oid bỏ đi octet index cuối cùng heuristic không
        # chắc; nhóm theo phần tiền tố tới mục bảng để dễ đọc).
        self.stdout.write(self.style.SUCCESS(f"Tổng {len(rows)} OID trả về dưới {parent}:\n"))
        count = 0
        col_samples: dict[str, int] = {}
        for oid, value in rows:
            # Tiền tố cột: bỏ phần sau dấu chấm cuối cùng nhóm 1 cấp (gần đúng).
            self.stdout.write(f"{oid} = {value}")
            count += 1
            # Thống kê tiền tố tới .1.1.<col> nếu có cấu trúc table .1.<col>.<idx>
            head = ".".join(oid.split(".")[: len(parent.split(".")) + 4])
            col_samples[head] = col_samples.get(head, 0) + 1
            if count >= limit:
                self.stdout.write(self.style.WARNING(
                    f"... (đã giới hạn {limit} dòng, dùng --limit để xem thêm)"))
                break

        self.stdout.write("")
        self.stdout.write(self.style.NOTICE("Gợi ý nhóm cột (prefix → số dòng):"))
        for head, n in sorted(col_samples.items(), key=lambda x: -x[1])[:20]:
            self.stdout.write(f"  {head}.*  → {n} dòng")
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            "Đối chiếu các prefix trên với section 'wlan.ap' / 'wlan.station' trong "
            "oids/huawei_vrp.yaml rồi chỉnh lại OID cho khớp thiết bị."
        ))
