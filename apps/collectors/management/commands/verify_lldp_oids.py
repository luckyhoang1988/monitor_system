"""Xác minh OID LLDP trên switch — đối chiếu neighbor với AP trên AC.

Usage:
    python manage.py verify_lldp_oids <device_id>
    python manage.py verify_lldp_oids <device_id> --ac <wlan_controller_id>
    python manage.py verify_lldp_oids --ip 10.0.193.1 --community public
    python manage.py verify_lldp_oids <device_id> --raw
"""
from django.core.management.base import BaseCommand, CommandError

from apps.collectors.topology_lldp import collect_lldp_neighbors, normalize_mac


class Command(BaseCommand):
    help = "Xác minh LLDP neighbor trên switch — in port, sysName, MAC để map AP."

    def add_arguments(self, parser):
        parser.add_argument("device_id", nargs="?", type=int,
                            help="ID switch trong DB.")
        parser.add_argument("--ip", help="IP switch (nếu không dùng device_id).")
        parser.add_argument("--community", default="public",
                            help="SNMP community khi dùng --ip.")
        parser.add_argument("--snmp-version", type=int, default=2, choices=[1, 2])
        parser.add_argument("--ac", type=int, default=None,
                            help="ID WLAN controller — đối chiếu MAC với WifiApStats.")
        parser.add_argument("--all-neighbors", action="store_true",
                            help="In mọi neighbor LLDP, không chỉ AP.")
        parser.add_argument("--raw", action="store_true",
                            help="In thêm chassis_id thô.")

    def handle(self, *args, **opts):
        device = self._resolve_device(opts)
        self.stdout.write(self.style.NOTICE(
            f"LLDP verify: {device.name} ({device.ip_address}) os_family={device.os_family or 'huawei_vrp'}"
        ))

        neighbors = collect_lldp_neighbors(
            device,
            ap_only=not opts["all_neighbors"],
        )
        if not neighbors:
            self.stdout.write(self.style.WARNING(
                "Không có neighbor LLDP (hoặc không phân loại được AP). "
                "Thử --all-neighbors hoặc kiểm tra SNMP view LLDP-MIB 1.0.8802.1.1.2."
            ))
            return

        ap_macs: dict[str, str] = {}
        if opts.get("ac"):
            ap_macs = self._load_ac_ap_macs(opts["ac"])

        self.stdout.write(self.style.SUCCESS(
            f"\n{'localPort':<28} {'sysName':<28} {'MAC':<20} {'portId':<16} AC_match"
        ))
        matched = 0
        for n in neighbors:
            mac = n.remote_mac or "—"
            ac_name = ""
            if mac and mac in ap_macs:
                ac_name = ap_macs[mac]
                matched += 1
            elif n.remote_sys_name:
                for m, name in ap_macs.items():
                    if name.lower() == n.remote_sys_name.lower():
                        ac_name = name
                        matched += 1
                        break
            self.stdout.write(
                f"{n.local_port:<28} {n.remote_sys_name[:28]:<28} {mac:<20} "
                f"{n.remote_port_id[:16]:<16} {ac_name}"
            )
            if opts["raw"] and n.remote_chassis_id:
                self.stdout.write(f"  chassis_raw: {ascii(n.remote_chassis_id)}")

        if ap_macs:
            pct = int(100 * matched / len(ap_macs)) if ap_macs else 0
            self.stdout.write(self.style.SUCCESS(
                f"\nĐối chiếu AC: {matched}/{len(neighbors)} neighbor khớp AP "
                f"({len(ap_macs)} AP trên AC, ~{pct}% coverage)."
            ))
        else:
            self.stdout.write(self.style.NOTICE(
                "\nThêm --ac <wlan_controller_id> để đối chiếu với danh sách AP trên AC."
            ))

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
            # Thiết bị tạm không trong DB
            class _Tmp:
                name = ip
                ip_address = ip
                os_family = "huawei_vrp"
                snmp_version = "v2c" if opts["snmp_version"] == 2 else "v1"
                snmp_community = opts["community"]
            return _Tmp()
        raise CommandError("Cần truyền device_id hoặc --ip.")

    def _load_ac_ap_macs(self, ac_id: int) -> dict[str, str]:
        from apps.devices.models import Device
        from apps.metrics.models import WifiApStats

        try:
            ac = Device.objects.get(pk=ac_id, device_type="wlan_controller")
        except Device.DoesNotExist as exc:
            raise CommandError(f"WLAN controller id={ac_id} không tồn tại.") from exc

        latest_ts = (
            WifiApStats.objects.filter(device=ac)
            .order_by("-timestamp")
            .values_list("timestamp", flat=True)
            .first()
        )
        if not latest_ts:
            self.stdout.write(self.style.WARNING(f"AC {ac.name}: chưa có WifiApStats."))
            return {}

        result: dict[str, str] = {}
        for ap in WifiApStats.objects.filter(device=ac, timestamp=latest_ts):
            mac = normalize_mac(ap.ap_mac)
            if mac:
                result[mac] = ap.ap_name
        return result
