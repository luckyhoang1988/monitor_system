"""Dò + in kết nối switch↔switch phát hiện qua FDB (SNMP live, KHÔNG ghi DB nếu --dry).

Hữu ích để đối chiếu cây topology với thực tế trước khi tin vào sơ đồ:
  - in registry MAC (mỗi switch có những MAC gì) — kiểm tra thu MAC có đủ không.
  - in các cặp switch nối trực tiếp (X:portX ↔ Y:portY) thuật toán giao-rỗng tìm ra.
  - --write: thực sự upsert TopologyLink (gọi discover_switch_links_fdb).

Usage:
    python manage.py verify_switch_links
    python manage.py verify_switch_links --macs     # in chi tiết MAC từng switch
    python manage.py verify_switch_links --write     # ghi link vào DB
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.collectors.topology_switch_fdb import (
    build_switch_mac_registry,
    discover_switch_adjacency,
)
from apps.devices.models import Device
from apps.devices.topology_hierarchy import bfs_depths, find_core_device


class Command(BaseCommand):
    help = "Dò kết nối switch↔switch qua FDB và in ra để đối chiếu thực tế."

    def add_arguments(self, parser):
        parser.add_argument("--macs", action="store_true",
                            help="In chi tiết MAC thuộc từng switch (registry).")
        parser.add_argument("--write", action="store_true",
                            help="Ghi link vào DB (gọi discover_switch_links_fdb).")

    def handle(self, *args, **opts):
        switches = list(
            Device.objects.filter(device_type="switch", enabled=True, protocol="snmp")
            .order_by("name")
        )
        if not switches:
            self.stdout.write(self.style.WARNING("Không có switch SNMP enabled nào."))
            return

        by_id = {sw.id: sw for sw in switches}
        self.stdout.write(f"Thu MAC từ {len(switches)} switch...")
        registry = build_switch_mac_registry(switches)
        self.stdout.write(self.style.SUCCESS(f"Registry: {len(registry)} MAC."))

        if opts.get("macs"):
            macs_per_sw: dict[int, list[str]] = {}
            for mac, dev_id in registry.items():
                macs_per_sw.setdefault(dev_id, []).append(mac)
            for sw in switches:
                macs = sorted(macs_per_sw.get(sw.id, []))
                self.stdout.write(f"\n{sw.name} ({sw.ip_address}) — {len(macs)} MAC:")
                for mac in macs:
                    self.stdout.write(f"  {mac}")

        pairs = discover_switch_adjacency(switches, registry)
        core = find_core_device()
        adjacency: dict[int, set[int]] = {}
        for a, _pa, b, _pb in pairs:
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        depths = bfs_depths(adjacency, core.id if core else None)

        self.stdout.write(self.style.SUCCESS(
            f"\n{len(pairs)} cặp switch nối trực tiếp"
            + (f" (core: {core.name})" if core else " (không tìm thấy core)") + ":"
        ))

        def depth_of(d: int) -> int:
            return depths.get(d, 10_000)

        for a, port_a, b, port_b in pairs:
            if depth_of(a) <= depth_of(b):
                parent, pport, child, cport = a, port_a, b, port_b
            else:
                parent, pport, child, cport = b, port_b, a, port_a
            pn = by_id[parent].name if parent in by_id else parent
            cn = by_id[child].name if child in by_id else child
            dp = depths.get(parent)
            dp_s = f"L{dp}" if dp is not None else "L?"
            self.stdout.write(
                f"  [{dp_s}] {pn}:{pport}  ↓trunk↓  {cn}:{cport}"
            )

        if opts.get("write"):
            from apps.collectors.topology_writer import discover_switch_links_fdb
            n = discover_switch_links_fdb(switches)
            self.stdout.write(self.style.SUCCESS(f"\nĐã ghi {n} link switch↔switch vào DB."))
        else:
            self.stdout.write(self.style.WARNING(
                "\n(dry-run — chưa ghi DB. Dùng --write để lưu link.)"
            ))
