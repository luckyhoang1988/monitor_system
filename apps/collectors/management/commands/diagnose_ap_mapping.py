"""Giải thích vì sao AP trên AC chưa map được vào topology (đọc DB, không cần SNMP live).

Tái hiện ĐÚNG logic orphan của apps/dashboard/topology_api.py để con số khớp dashboard:
  - mapped_macs = MAC của AP TopologyLink (link_kind=ap, is_stale=False) đã chuẩn hóa.
  - AP trên AC mà MAC không nằm trong mapped_macs → orphan ("Chưa map").

Với mỗi AP orphan, dò nguyên nhân: có LLDP/FDB link trùng TÊN nhưng lệch MAC?
link bị stale? hay không có neighbor nào (port không chạy LLDP / switch không monitor)?

Usage:
    python manage.py diagnose_ap_mapping
    python manage.py diagnose_ap_mapping --ac 16
    python manage.py diagnose_ap_mapping --links   # in thêm toàn bộ AP link đã lưu
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.collectors.topology_lldp import normalize_mac
from apps.devices.models import TopologyLink
from apps.devices.topology_match import get_default_ac_device, load_ac_ap_snapshot


class Command(BaseCommand):
    help = "Chẩn đoán AP chưa map vào topology — đọc DB, đối chiếu AC ↔ TopologyLink."

    def add_arguments(self, parser):
        parser.add_argument("--ac", type=int, default=None,
                            help="ID WLAN controller (mặc định: AC đầu tiên).")
        parser.add_argument("--links", action="store_true",
                            help="In thêm toàn bộ AP TopologyLink đã lưu (switch:port → AP).")

    def handle(self, *args, **opts):
        ac = self._resolve_ac(opts.get("ac"))
        if not ac:
            raise CommandError("Không có WLAN controller nào (device_type=wlan_controller).")

        snapshot = load_ac_ap_snapshot(ac)  # {mac: {ap_name, ap_ip, is_online, client_count}}
        if not snapshot:
            self.stdout.write(self.style.WARNING(
                f"AC {ac.name}: chưa có WifiApStats snapshot — chưa poll AC lần nào?"
            ))
            return

        # mapped_macs: y hệt topology_api.build_topology_graph
        ap_links = list(
            TopologyLink.objects.filter(link_kind="ap", is_stale=False)
            .select_related("local_device")
        )
        mapped_macs = {
            normalize_mac(link.remote_ap_mac)
            for link in ap_links
            if normalize_mac(link.remote_ap_mac)
        }

        # Mọi link AP (kể cả stale) đánh index theo MAC + theo tên để dò nguyên nhân.
        all_links = list(
            TopologyLink.objects.filter(link_kind="ap").select_related("local_device")
        )
        by_name: dict[str, list[TopologyLink]] = {}
        for link in all_links:
            for nm in (link.remote_ap_name, link.remote_sys_name):
                key = (nm or "").strip().casefold()
                if key:
                    by_name.setdefault(key, []).append(link)

        orphans = []
        for mac, info in snapshot.items():
            if mac not in mapped_macs:
                orphans.append((mac, info))

        self.stdout.write(self.style.NOTICE(
            f"AC: {ac.name} (id={ac.id}) — {len(snapshot)} AP có MAC trong snapshot"
        ))
        self.stdout.write(
            f"AP link (link_kind=ap, không stale): {len(ap_links)}  ·  "
            f"MAC đã map: {len(mapped_macs)}  ·  orphan: {len(orphans)}"
        )

        if not orphans:
            self.stdout.write(self.style.SUCCESS("\nTất cả AP trên AC đều đã map. 🎉"))
        else:
            self.stdout.write(self.style.WARNING(
                f"\n=== {len(orphans)} AP CHƯA MAP — chẩn đoán ==="
            ))
            for mac, info in sorted(orphans, key=lambda x: x[1]["ap_name"]):
                name = info["ap_name"] or "(không tên)"
                ip = info["ap_ip"] or "—"
                state = "online" if info["is_online"] else "OFFLINE"
                self.stdout.write(
                    f"\n• {name}  mac={mac}  ip={ip}  [{state}]"
                )
                self.stdout.write("    " + self._diagnose(mac, name, by_name))

        if opts["links"]:
            self._print_links(ap_links)

    def _diagnose(self, mac: str, name: str, by_name: dict) -> str:
        """Tìm lý do AP này không vào mapped_macs."""
        key = (name or "").strip().casefold()
        cand = by_name.get(key, [])
        if cand:
            link = cand[0]
            link_mac = normalize_mac(link.remote_ap_mac) or "(rỗng)"
            where = f"{link.local_device.name}:{link.local_port}"
            if link.is_stale:
                return (self.style.WARNING(
                    f"LLDP/FDB CÓ thấy AP này tại {where} nhưng link đã STALE "
                    f"(miss>={3}) → switch hết báo neighbor. Kiểm tra LLDP/cáp port {link.local_port}."
                ))
            if link_mac != mac:
                return (self.style.ERROR(
                    f"LLDP thấy tại {where} nhưng MAC LỆCH: link.mac={link_mac} ≠ AC.mac={mac}. "
                    f"AP báo MAC ethernet (chassis-id) khác MAC radio trên AC → match 'mac' fail. "
                    f"match_method='{link.match_method}', confirmed={link.is_confirmed}. "
                    f"Cần khớp theo tên/IP hoặc map chassis→radio."
                ))
            return (
                f"Có link tại {where} mac trùng nhưng không vào mapped "
                f"(kiểm tra is_stale={link.is_stale})."
            )
        return (self.style.WARNING(
            "KHÔNG có TopologyLink nào trùng tên/MAC → switch không báo neighbor "
            "(port AP không bật LLDP, hoặc switch chứa AP không được monitor SNMP, "
            "hoặc AP cắm thẳng vào core/thiết bị ngoài fleet)."
        ))

    def _print_links(self, ap_links) -> None:
        self.stdout.write(self.style.SUCCESS("\n=== AP TopologyLink (switch:port → AP) ==="))
        self.stdout.write(
            f"{'switch':<22} {'port':<18} {'AP name':<26} {'mac':<18} {'method':<8} conf"
        )
        for link in sorted(ap_links, key=lambda x: (x.local_device.name, x.local_port)):
            self.stdout.write(
                f"{link.local_device.name[:22]:<22} {link.local_port[:18]:<18} "
                f"{(link.remote_ap_name or link.remote_sys_name)[:26]:<26} "
                f"{(normalize_mac(link.remote_ap_mac) or '—'):<18} "
                f"{link.match_method:<8} {'Y' if link.is_confirmed else 'n'}"
            )

    def _resolve_ac(self, ac_id):
        from apps.devices.models import Device

        if ac_id:
            try:
                return Device.objects.get(pk=ac_id, device_type="wlan_controller")
            except Device.DoesNotExist as exc:
                raise CommandError(f"WLAN controller id={ac_id} không tồn tại.") from exc
        return get_default_ac_device()
