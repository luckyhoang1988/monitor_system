"""PingCollector — thu thập trạng thái hoạt động cơ bản qua giao thức Ping/ICMP."""
import logging
from datetime import datetime, timezone
from .base import BaseCollector, NormalizedData
from .ping_util import icmp_ping

logger = logging.getLogger(__name__)


class PingCollector(BaseCollector):
    def test_connection(self) -> str:
        """Kiểm tra kết nối ping và trả về os_family giả lập."""
        success, rtt = self._ping()
        if success:
            return "ping_only"
        raise Exception(f"Không thể kết nối Ping tới thiết bị {self.device.ip_address} (Timeout)")

    def collect_raw(self) -> dict:
        """Thu thập kết quả ping thô."""
        success, rtt = self._ping()
        return {
            "success": success,
            "rtt_ms": rtt if success else None,
        }

    def adapt(self, raw: dict) -> NormalizedData:
        """Chuẩn hóa kết quả quét sang định dạng NormalizedData."""
        success = raw.get("success", False)
        rtt = raw.get("rtt_ms")

        # Ping_only sẽ không có CPU/RAM thực tế, set bằng 0.0 nếu online và -1.0 nếu offline
        cpu_val = 0.0 if success else -1.0
        mem_val = 0.0 if success else -1.0
        uptime = 86400 if success else 0

        return NormalizedData(
            device_name=self.device.name,
            ip_address=self.device.ip_address,
            timestamp=datetime.now(timezone.utc),
            os_family="ping_only",
            cpu_percent=cpu_val,
            mem_percent=mem_val,
            uptime_secs=uptime,
            interfaces=[],
            extra={"ping_rtt_ms": rtt} if success else {},
        )

    def _ping(self) -> tuple[bool, float]:
        """Thực hiện ICMP ping qua util dùng chung."""
        success, rtt = icmp_ping(self.device.ip_address)
        return success, (rtt if rtt is not None else 0.0)
