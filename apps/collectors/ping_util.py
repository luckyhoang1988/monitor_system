"""ICMP ping dùng chung — cross-platform (Windows/Linux), tái sử dụng cho
PingCollector và bước xác định online (ICMP + SNMP) trong poll task.
"""
import sys
import re
import subprocess
import logging

logger = logging.getLogger(__name__)


def _parse_rtt(output: str) -> float:
    """Trích RTT (ms) từ stdout của lệnh ping; trả 1.0 nếu thông nhưng không parse được."""
    try:
        m = re.search(r"time[=<]\s*([\d.]+)\s*(ms)?", output, re.IGNORECASE)
        if m:
            return float(m.group(1))
        avg = re.search(r"Average\s*=\s*([\d.]+)\s*(ms)?", output, re.IGNORECASE)
        if avg:
            return float(avg.group(1))
    except (ValueError, TypeError):
        pass
    return 1.0


def icmp_ping(ip: str, timeout_secs: int = 1, attempts: int = 2) -> tuple[bool, float | None]:
    """Ping ICMP tới ip. Trả (success, rtt_ms).

    Thử tối đa `attempts` lần (mỗi lần 1 gói) để giảm false-negative do mất gói lẻ.
    Thành công ngay khi 1 lần phản hồi. rtt_ms = None khi thất bại.
    """
    timeout_secs = max(int(timeout_secs or 1), 1)
    for _ in range(max(int(attempts or 1), 1)):
        if sys.platform.startswith("win"):
            cmd = ["ping", "-n", "1", "-w", str(timeout_secs * 1000), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(timeout_secs), ip]
        try:
            res = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=timeout_secs + 2,
            )
            if res.returncode == 0:
                return True, _parse_rtt(res.stdout)
        except FileNotFoundError:
            logger.error("Không tìm thấy lệnh 'ping' trên hệ thống (cần cài iputils-ping)")
            return False, None
        except Exception as exc:  # subprocess.TimeoutExpired, OSError...
            logger.debug("Ping %s lỗi: %s", ip, exc)
    return False, None
