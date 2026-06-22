"""Định danh Interface theo (device, name) thay cho (device, if_index).

SSH collector sinh if_index theo vị trí block CLI nên không ổn định giữa các poll,
gây tạo Interface trùng + mất lịch sử counter. Đổi khoá duy nhất sang tên cổng.

Bước 1: gộp các Interface trùng (cùng device + name) — repoint mọi stats con về hàng
canonical (pk nhỏ nhất) rồi xoá hàng dư, nếu không AlterUniqueTogether sẽ fail.
Bước 2: đổi unique_together sang (device, name).
"""
from django.db import migrations


def merge_duplicate_interfaces(apps, schema_editor):
    Interface = apps.get_model("devices", "Interface")
    InterfaceStats = apps.get_model("metrics", "InterfaceStats")
    InterfaceStatsHourly = apps.get_model("metrics", "InterfaceStatsHourly")
    InterfaceStatsDaily = apps.get_model("metrics", "InterfaceStatsDaily")

    # Gom theo (device_id, name chuẩn hoá) → danh sách pk (đã sort tăng dần).
    groups: dict[tuple[int, str], list[int]] = {}
    for pk, device_id, name in Interface.objects.order_by("pk").values_list("pk", "device_id", "name"):
        key = (device_id, (name or "").strip().casefold())
        groups.setdefault(key, []).append(pk)

    for pks in groups.values():
        if len(pks) < 2:
            continue
        canonical, *dups = pks  # pk nhỏ nhất giữ lại
        InterfaceStats.objects.filter(interface_id__in=dups).update(interface_id=canonical)
        InterfaceStatsHourly.objects.filter(interface_id__in=dups).update(interface_id=canonical)
        InterfaceStatsDaily.objects.filter(interface_id__in=dups).update(interface_id=canonical)
        Interface.objects.filter(pk__in=dups).delete()


def noop_reverse(apps, schema_editor):
    # Không khôi phục được các hàng đã gộp; reverse chỉ trả constraint.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("devices", "0009_alter_device_device_type_alter_device_vendor"),
        ("metrics", "0004_alter_vmstats_repl_health"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_interfaces, noop_reverse),
        migrations.AlterUniqueTogether(
            name="interface",
            unique_together={("device", "name")},
        ),
    ]
