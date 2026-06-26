"""Xác minh OID trunk/access (Q-BRIDGE) trên switch trước khi tin collector.

Walk dot1qVlanStaticEgressPorts / dot1qVlanStaticUntaggedPorts / dot1dBasePortIfIndex
/ dot1qPvid, tính port_mode (access/trunk/hybrid) cho từng cổng rồi in cạnh ifName để
đối chiếu `display port vlan` (Huawei) / `show interfaces switchport` (Cisco).

Luu y: Bitmap PortList la OCTET STRING - easysnmp co the cat tai null byte. Lenh in ca RAW
value (ascii repr) de phat hien truong hop nay.

Usage:
    python manage.py verify_vlan_oids <device_id>
    python manage.py verify_vlan_oids --ip 10.0.193.10 --community public --snmp-version 2
"""
from django.core.management.base import BaseCommand, CommandError

# Q-BRIDGE-MIB / BRIDGE-MIB — chuẩn, đa hãng (giống nhánh `vlan:` trong oids/*.yaml).
OID_IF_DESCR        = "1.3.6.1.2.1.2.2.1.2"
OID_STATIC_EGRESS   = "1.3.6.1.2.1.17.7.1.4.3.1.2"
OID_STATIC_UNTAGGED = "1.3.6.1.2.1.17.7.1.4.3.1.4"
OID_BASEPORT_IFIDX  = "1.3.6.1.2.1.17.1.4.1.2"
OID_DOT1Q_PVID      = "1.3.6.1.2.1.17.7.1.4.5.1.1"
# CISCO-VTP-MIB::vlanTrunkPortDynamicStatus — index = ifIndex; trunking(1)/notTrunking(2).
OID_VTP_TRUNK_STATUS = "1.3.6.1.4.1.9.9.46.1.6.1.1.14"


class Command(BaseCommand):
    help = "Xác minh OID Q-BRIDGE để phân loại trunk/access từng cổng switch."

    def add_arguments(self, parser):
        parser.add_argument("device_id", nargs="?", type=int,
                            help="ID của Device (switch) đã lưu trong DB.")
        parser.add_argument("--ip", help="IP thiết bị (nếu không dùng device_id).")
        parser.add_argument("--community", default="public",
                            help="SNMP community (chỉ dùng khi truyền --ip).")
        parser.add_argument("--snmp-version", type=int, default=2, choices=[1, 2],
                            help="SNMP version khi dùng --ip (1 hoặc 2c).")
        parser.add_argument("--raw", action="store_true",
                            help="In thêm RAW value của từng bitmap VLAN (debug parse).")

    def handle(self, *args, **opts):
        from apps.collectors.snmp_client import (
            create_snmp_session,
            resolve_snmp_backend,
            snmp_walk_pairs,
        )
        from apps.collectors.switch_snmp import _parse_portlist

        snmp_kwargs, target = self._resolve_target(opts)
        backend = resolve_snmp_backend()
        self.stdout.write(self.style.NOTICE(f"SNMP backend={backend} | target={target}"))
        session = create_snmp_session(snmp_kwargs, backend=backend)

        # ifIndex → ifName
        if_names = {oid.split(".")[-1]: val for oid, val in snmp_walk_pairs(session, OID_IF_DESCR)}

        # Cisco — CISCO-VTP-MIB vlanTrunkPortDynamicStatus (index = ifIndex). Nếu có data,
        # đây là nguồn chính cho Cisco IOS/IOS-XE (collector ưu tiên trước Q-BRIDGE).
        vtp_rows = snmp_walk_pairs(session, OID_VTP_TRUNK_STATUS)
        if vtp_rows:
            self.stdout.write(self.style.SUCCESS(
                f"\n[CISCO-VTP] vlanTrunkPortDynamicStatus: {len(vtp_rows)} cổng (1=trunk, 2=access)"
            ))
            self.stdout.write(f"{'ifIndex':>8} {'ifName':<24} {'status':>6} mode")
            for oid, val in vtp_rows:
                ifidx = oid.split(".")[-1]
                mode = {"1": "trunk", "2": "access"}.get(str(val).strip(), "?")
                self.stdout.write(f"{ifidx:>8} {if_names.get(ifidx, '?'):<24} {str(val):>6} {mode}")
        else:
            self.stdout.write(self.style.NOTICE(
                "\n[CISCO-VTP] không có data (đúng cho Huawei / Cisco Business) → xem Q-BRIDGE bên dưới."
            ))

        # dot1dBasePort → ifIndex
        base_to_ifidx: dict[int, int] = {}
        for oid, val in snmp_walk_pairs(session, OID_BASEPORT_IFIDX):
            try:
                base_to_ifidx[int(oid.split(".")[-1])] = int(val)
            except (ValueError, TypeError):
                continue

        # PVID theo dot1dBasePort
        pvid_by_base: dict[int, int] = {}
        for oid, val in snmp_walk_pairs(session, OID_DOT1Q_PVID):
            try:
                pvid_by_base[int(oid.split(".")[-1])] = int(val)
            except (ValueError, TypeError):
                continue

        egress_rows  = snmp_walk_pairs(session, OID_STATIC_EGRESS)
        untagged_rows = snmp_walk_pairs(session, OID_STATIC_UNTAGGED)

        if not base_to_ifidx or not egress_rows:
            self.stdout.write(self.style.ERROR(
                "Thiếu dữ liệu Q-BRIDGE. Kiểm tra: SNMP view mở nhánh 1.3.6.1.2.1.17, "
                "community/version, ACL SNMP cho server giám sát."
            ))
            return

        egress_by_vlan  = {oid.split(".")[-1]: _parse_portlist(val) for oid, val in egress_rows}
        untagged_by_vlan = {oid.split(".")[-1]: _parse_portlist(val) for oid, val in untagged_rows}

        if opts["raw"]:
            self.stdout.write(self.style.NOTICE("\nRAW egress bitmaps:"))
            for oid, val in egress_rows:
                self.stdout.write(f"  {oid} = {ascii(str(val))} -> ports {sorted(_parse_portlist(val))}")

        # Đếm tagged/untagged theo dot1dBasePort.
        tagged_count: dict[int, int] = {}
        untagged_count: dict[int, int] = {}
        tagged_vlans: dict[int, list[str]] = {}
        access_vlan: dict[int, str] = {}
        for vlan, egress_ports in egress_by_vlan.items():
            untagged_ports = untagged_by_vlan.get(vlan, set())
            for port in egress_ports:
                if port in untagged_ports:
                    untagged_count[port] = untagged_count.get(port, 0) + 1
                    access_vlan[port] = vlan
                else:
                    tagged_count[port] = tagged_count.get(port, 0) + 1
                    tagged_vlans.setdefault(port, []).append(vlan)

        def mode_of(base_port: int) -> str:
            t, u = tagged_count.get(base_port, 0), untagged_count.get(base_port, 0)
            if t >= 1:
                return "trunk"
            if u >= 2:
                return "hybrid"
            if u == 1:
                return "access"
            return "—"

        self.stdout.write(self.style.SUCCESS(
            f"\n{'basePort':>8} {'ifIndex':>8} {'ifName':<24} {'PVID':>5} {'mode':<7} tagged_vlans"
        ))
        for base_port in sorted(base_to_ifidx):
            ifidx = base_to_ifidx[base_port]
            name = if_names.get(str(ifidx), "?")
            pvid = pvid_by_base.get(base_port, "")
            tv = ",".join(tagged_vlans.get(base_port, [])[:8])
            self.stdout.write(
                f"{base_port:>8} {ifidx:>8} {name:<24} {str(pvid):>5} {mode_of(base_port):<7} {tv}"
            )

        self.stdout.write(self.style.SUCCESS(
            "\nĐối chiếu cột 'mode' với `display port vlan` / `show interfaces switchport`. "
            "Cổng nối switch khác phải ra 'trunk'; cổng access ra đúng PVID."
        ))

    def _resolve_target(self, opts) -> tuple[dict, str]:
        device_id, ip = opts.get("device_id"), opts.get("ip")
        if device_id:
            from apps.devices.models import Device
            try:
                device = Device.objects.get(pk=device_id)
            except Device.DoesNotExist as exc:
                raise CommandError(f"Device id={device_id} không tồn tại.") from exc
            version = {"v1": 1, "v2c": 2}.get(device.snmp_version, 2)
            return (
                {"hostname": device.ip_address, "version": version,
                 "community": device.snmp_community, "timeout": 10, "retries": 2},
                f"{device.name} ({device.ip_address})",
            )
        if ip:
            return (
                {"hostname": ip, "version": opts["snmp_version"],
                 "community": opts["community"], "timeout": 10, "retries": 2},
                ip,
            )
        raise CommandError("Cần truyền device_id hoặc --ip.")
