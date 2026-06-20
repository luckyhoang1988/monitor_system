"""PingCollector — thu thập trạng thái hoạt động cơ bản qua giao thức Ping/ICMP."""
import sys
import subprocess
import re
import logging
from datetime import datetime, timezone
from .base import BaseCollector, NormalizedData

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
        """Thực hiện lệnh ping hệ thống."""
        ip = self.device.ip_address
        if sys.platform.startswith("win"):
            # Windows ping: -n 1 (1 gói), -w 1000 (timeout 1s)
            cmd = ["ping", "-n", "1", "-w", "1000", ip]
        else:
            # Linux ping: -c 1 (1 gói), -W 1 (timeout 1s)
            cmd = ["ping", "-c", "1", "-W", "1", ip]

        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
            if res.returncode != 0:
                return False, 0.0
            
            rtt = self._parse_rtt(res.stdout)
            return True, rtt
        except Exception as exc:
            logger.error("Ping error for %s: %s", ip, exc)
            return False, 0.0

    def _parse_rtt(self, output: str) -> float:
        """Trích xuất RTT từ stdout của lệnh ping."""
        try:
            # Tìm dạng time=XXms hoặc time=XX.XX ms
            match = re.search(r"time[=<]\s*([\d\.]+)\s*(ms)?", output, re.IGNORECASE)
            if match:
                return float(match.group(1))
            # Fallback trên Windows "Average = XXms"
            avg_match = re.search(r"Average\s*=\s*([\d\.]+)\s*(ms)?", output, re.IGNORECASE)
            if avg_match:
                return float(avg_match.group(1))
        except Exception:
            pass
        return 1.0  # Mặc định trả về 1.0 ms nếu kết nối thông nhưng không parse được RTT
