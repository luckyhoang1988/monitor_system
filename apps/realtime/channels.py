"""Tên kênh Redis pub/sub — single source of truth cho producer (Celery worker)
và consumer (ASGI SSE view). Cả hai phải import từ đây để luôn khớp.
"""

# Kênh tổng cho dashboard index (mọi thiết bị).
FLEET_CHANNEL = "events:fleet"


def device_channel(device_id: int) -> str:
    """Kênh riêng cho trang chi tiết 1 thiết bị."""
    return f"events:device:{device_id}"
