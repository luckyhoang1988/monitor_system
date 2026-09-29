"""Tests cho `manage.py seed_alert_rules --metric-prefix` — regression cho bug review ngoài
2026-09-29 (vòng 2): seed toàn bộ DEFAULT_RULES vô điều kiện ở entrypoint.sh (chạy mỗi lần app
khởi động) từng tự tạo lại BẤT KỲ rule mặc định nào đã bị người dùng xoá chủ động qua UI (seed
chỉ so khớp theo tên, không phân biệt được "chưa từng tạo" với "đã xoá chủ ý"). Fix: cờ
`--metric-prefix` giới hạn seed đúng phạm vi rule cần tự động đảm bảo tồn tại (iLO/RAID), không
đụng các rule khác dù chúng bị xoá."""
import pytest
from io import StringIO

from django.core.management import call_command

from apps.alerts.management.commands.seed_alert_rules import DEFAULT_RULES
from apps.alerts.models import AlertRule


@pytest.mark.django_db
class TestSeedAlertRulesMetricPrefix:
    """⚠️ DB test KHÔNG rỗng ngay cả trước khi gọi `seed_alert_rules` — migration
    `0004_alertconfig`/`0005_seed_wifi_ap_offline_rule` tự tạo sẵn 2 rule ("Thiết bị offline",
    "AP offline (dưới WLAN AC)") độc lập với `DEFAULT_RULES`/`seed_alert_rules.py`. Assertion
    dưới đây lọc theo tên `DEFAULT_RULES` (không đếm tổng `AlertRule.objects.count()`) để không
    nhầm 2 rule đó thành kết quả của lệnh seed."""

    def test_no_prefix_seeds_all_default_rules(self):
        call_command("seed_alert_rules", "--channels", "telegram", stdout=StringIO())

        default_names = {r["name"] for r in DEFAULT_RULES}
        created_names = set(
            AlertRule.objects.filter(name__in=default_names).values_list("name", flat=True)
        )
        assert created_names == default_names

    def test_metric_prefix_only_seeds_matching_rules(self):
        call_command(
            "seed_alert_rules", "--channels", "telegram",
            "--metric-prefix", "ilo_", "raid_", stdout=StringIO(),
        )

        matching_names = {r["name"] for r in DEFAULT_RULES if r["metric"].startswith(("ilo_", "raid_"))}
        created_matching = set(
            AlertRule.objects.filter(name__in=matching_names).values_list("name", flat=True)
        )
        assert created_matching == matching_names
        # "Switch CPU Critical" chỉ có thể được tạo bởi seed_alert_rules (không có trong migration
        # fixture nào) — vẫn KHÔNG tồn tại xác nhận filter --metric-prefix hoạt động đúng.
        assert not AlertRule.objects.filter(name="Switch CPU Critical").exists()

    def test_metric_prefix_does_not_resurrect_deleted_non_matching_rule(self):
        """Bug tái hiện đúng theo báo cáo: rule "Switch CPU Critical" (không phải iLO/RAID) bị
        xoá chủ ý qua UI (`rule_delete`) -> seed với `--metric-prefix ilo_ raid_` (mô phỏng lệnh
        entrypoint.sh chạy mỗi lần app khởi động) KHÔNG được tạo lại nó."""
        call_command("seed_alert_rules", "--channels", "telegram", stdout=StringIO())
        assert AlertRule.objects.filter(name="Switch CPU Critical").exists()

        AlertRule.objects.get(name="Switch CPU Critical").delete()

        call_command(
            "seed_alert_rules", "--channels", "telegram",
            "--metric-prefix", "ilo_", "raid_", stdout=StringIO(),
        )

        assert not AlertRule.objects.filter(name="Switch CPU Critical").exists()

    def test_metric_prefix_still_recreates_deleted_matching_rule(self):
        """Đối chứng: rule NẰM TRONG phạm vi prefix (vd iLO) bị xoá vẫn được seed tạo lại —
        đây là hành vi ĐÚNG Ý (đảm bảo 13 rule iLO/RAID luôn tồn tại sau deploy), không phải bug."""
        call_command("seed_alert_rules", "--channels", "telegram", stdout=StringIO())
        AlertRule.objects.get(name="HyperV RAID Missing Disk").delete()

        call_command(
            "seed_alert_rules", "--channels", "telegram",
            "--metric-prefix", "ilo_", "raid_", stdout=StringIO(),
        )

        assert AlertRule.objects.filter(name="HyperV RAID Missing Disk").exists()
