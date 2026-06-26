"""Xác minh topology trên switch — LLDP + bảng MAC (FDB), đối chiếu AP trên AC.

Usage:
    python manage.py verify_lldp_oids <device_id>
    python manage.py verify_lldp_oids 17 --ac <wlan_controller_id>
    python manage.py verify_lldp_oids 17 --fdb-only
    python manage.py verify_lldp_oids 17 --show-all-macs
"""
from django.core.management.base import BaseCommand, CommandError

from apps.collectors.topology_fdb import collect_switch_mac_table
from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac
from apps.devices.topology_match import get_default_ac_device, load_ac_ap_snapshot


class Command(BaseCommand):
    help = "Xác minh LLDP / bảng MAC switch — đối chiếu MAC với AP trên AC."

    def add_arguments(self, parser):
        parser.add_argument("device_id", nargs="?", type=int,
                            help="ID switch trong DB.")
        parser.add_argument("--ip", help="IP switch (nếu không dùng device_id).")
        parser.add_argument("--community", default="public",
                            help="SNMP community khi dùng --ip.")
        parser.add_argument("--snmp-version", type=int, default=2, choices=[1, 2])
        parser.add_argument("--ac", type=int, default=None,
                            help="ID WLAN controller (mặc định: AC đầu tiên).")
        parser.add_argument("--all-neighbors", action="store_true",
                            help="LLDP: in mọi neighbor, không chỉ AP.")
        parser.add_argument("--fdb-only", action="store_true",
                            help="Chỉ walk bảng MAC (FDB), bỏ qua LLDP.")
        parser.add_argument("--show-all-macs", action="store_true",
                            help="FDB: in mọi MAC trên switch, không chỉ AP.")
        parser.add_argument("--raw", action="store_true",
                            help="In thêm chassis_id thô (LLDP).")

    def handle(self, *args, **opts):
        device = self._resolve_device(opts)
        ac = self._resolve_ac(opts.get("ac"))
        ap_snapshot = load_ac_ap_snapshot(ac)
        ap_macs = set(ap_snapshot.keys())

        self.stdout.write(self.style.NOTICE(
            f"Topology verify: {device.name} ({device.ip_address}) "
            f"os_family={device.os_family or 'huawei_vrp'}"
        ))
        if ac:
            self.stdout.write(self.style.NOTICE(
                f"AC: {ac.name} — {len(ap_snapshot)} AP có MAC trong snapshot mới nhất"
            ))
        else:
            self.stdout.write(self.style.WARNING(
                "Không có WLAN controller — chỉ in MAC switch, không đối chiếu AC."
            ))

        if not opts["fdb_only"]:
            self._print_lldp(device, ap_snapshot, opts)

        self._print_fdb(device, ap_macs, ap_snapshot, opts)

        if ap_snapshot:
            self._print_ac_unmatched(device, ap_macs, ap_snapshot, opts)

    def _print_lldp(self, device, ap_snapshot: dict, opts) -> None:
        neighbors = collect_lldp_neighbors(
            device,
            ap_only=not opts["all_neighbors"],
        )
        self.stdout.write(self.style.SUCCESS("\n=== LLDP neighbor ==="))
        if not neighbors:
            self.stdout.write(self.style.WARNING(
                "Không có neighbor LLDP qua SNMP (1.0.8802.1.1.2). "
                "Xem bảng MAC bên dưới hoặc bật LLDP + mở SNMP view."
            ))
            return

        self.stdout.write(
            f"{'localPort':<24} {'sysName':<26} {'MAC':<20} {'AC_AP':<28} {'match'}"
        )
        matched_macs: set[str] = set()
        for n in neighbors:
            mac = normalize_mac(n.remote_mac) or "—"
            ac_name = ""
            match = "—"
            if mac in ap_snapshot:
                ac_name = ap_snapshot[mac]["ap_name"]
                match = "MAC"
                matched_macs.add(mac)
            elif n.remote_sys_name:
                for m, info in ap_snapshot.items():
                    if info["ap_name"].lower() == n.remote_sys_name.lower():
                        ac_name = info["ap_name"]
                        match = "name"
                        matched_macs.add(m)
                        break
            self.stdout.write(
                f"{n.local_port:<24} {n.remote_sys_name[:26]:<26} {mac:<20} "
                f"{ac_name[:28]:<28} {match}"
            )
            if opts["raw"] and n.remote_chassis_id:
                self.stdout.write(f"  chassis_raw: {ascii(n.remote_chassis_id)}")

        if ap_snapshot:
            self.stdout.write(self.style.SUCCESS(
                f"LLDP khớp AC: {len(matched_macs)}/{len(ap_snapshot)} AP"
            ))

    def _print_fdb(self, device, ap_macs: set[str], ap_snapshot: dict, opts) -> None:
        show_all = opts["show_all_macs"] or not ap_macs
        entries = collect_switch_mac_table(
            device,
            ap_macs=ap_macs if ap_macs else None,
            ap_only=not show_all and bool(ap_macs),
        )
        self.stdout.write(self.style.SUCCESS("\n=== Bảng MAC switch (FDB) ==="))
        if not entries:
            self.stdout.write(self.style.WARNING(
                "Không đọc được FDB (1.3.6.1.2.1.17.7.1.2.2 / 17.4.3). "
                "Kiểm tra SNMP view nhánh 1.3.6.1.2.1.17."
            ))
            return

        self.stdout.write(
            f"{'port':<20} {'MAC':<20} {'vlan':>5} {'AC_AP':<28} {'online':<8} match"
        )
        matched_macs: set[str] = set()
        for e in entries:
            ac_name = ""
            online = "—"
            match = "AP" if e.is_ap_match else "—"
            if e.mac in ap_snapshot:
                info = ap_snapshot[e.mac]
                ac_name = info["ap_name"]
                online = "on" if info["is_online"] else "off"
                matched_macs.add(e.mac)
                match = "MAC"
            vlan = str(e.vlan_id) if e.vlan_id is not None else "—"
            self.stdout.write(
                f"{e.local_port:<20} {e.mac:<20} {vlan:>5} "
                f"{ac_name[:28]:<28} {online:<8} {match}"
            )

        if ap_snapshot:
            self.stdout.write(self.style.SUCCESS(
                f"FDB khớp AC: {len(matched_macs)}/{len(ap_snapshot)} AP trên switch này"
            ))

    def _print_ac_unmatched(
        self, device, ap_macs: set[str], ap_snapshot: dict, opts,
    ) -> None:
        entries = collect_switch_mac_table(
            device, ap_macs=ap_macs, ap_only=True,
        )
        found = {e.mac for e in entries}
        missing = [m for m in ap_snapshot if m not in found]
        if not missing:
            self.stdout.write(self.style.SUCCESS(
                f"\nTất cả {len(ap_snapshot)} AP trên AC đều có MAC trên switch này."
            ))
            return
        self.stdout.write(self.style.WARNING(
            f"\n=== AP trên AC chưa thấy MAC trên {device.name} ({len(missing)}) ==="
        ))
        for mac in sorted(missing):
            info = ap_snapshot[mac]
            self.stdout.write(
                f"  {info['ap_name']:<28} {mac}  ip={info['ap_ip'] or '—'}"
            )

    def _resolve_ac(self, ac_id: int | None):
        from apps.devices.models import Device

        if ac_id:
            try:
                return Device.objects.get(pk=ac_id, device_type="wlan_controller")
            except Device.DoesNotExist as exc:
                raise CommandError(f"WLAN controller id={ac_id} không tồn tại.") from exc
        return get_default_ac_device()

    def _resolve_device(self, opts):
        device_id = opts.get("device_id")
        if device_id:
            from apps.devices.models import Device
            try:
                return Device.objects.get(pk=device_id)
            except Device.DoesNotExist as exc:
                raise CommandError(f"Device id={device_id} không tồn tại.") from exc
        ip = opts.get("ip")
        if ip:
            from apps.devices.models import Device
            device = Device.objects.filter(ip_address=ip).first()
            if device:
                return device
            class _Tmp:
                name = ip
                ip_address = ip
                os_family = "huawei_vrp"
                snmp_version = "v2c" if opts["snmp_version"] == 2 else "v1"
                snmp_community = opts["community"]
            return _Tmp()
        raise CommandError("Cần truyền device_id hoặc --ip.")
