"""Producer phía Celery worker: phát event metrics lên Redis pub/sub sau khi
1 thiết bị poll xong. Dùng redis client ĐỒNG BỘ (worker không phải async).

Nguyên tắc: publish KHÔNG BAO GIỜ được làm fail/retry poll — mọi lỗi đều bị
nuốt + log warning. Redis chết ⇒ chỉ mất realtime, poll vẫn chạy bình thường.
"""

import json
import logging
import time
from typing import TYPE_CHECKING

import redis
from django.conf import settings

from .channels import FLEET_CHANNEL, device_channel

if TYPE_CHECKING:
    from apps.collectors.base import NormalizedData
    from apps.devices.models import Device

logger = logging.getLogger(__name__)

_client: "redis.Redis | None" = None


def _get_client() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.REALTIME_REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client


def build_payload(device: "Device", online: bool, data: "NormalizedData | None") -> dict:
    """Dựng payload compact từ dữ liệu in-memory (khớp cái writer vừa ghi).

    `data=None` ở nhánh ICMP-down → cpu/mem null, if_total=0.
    """
    last_seen = device.last_seen.timestamp() if device.last_seen else None
    payload = {
        "v": 1,
        "type": "metrics",
        "device_id": device.pk,
        "name": device.name,
        "device_type": device.device_type,
        "online": bool(online),
        "last_seen": last_seen,
        "cpu": None,
        "mem": None,
        "if_up": 0,
        "if_total": 0,
        "ts": time.time(),
    }
    if data is not None:
        payload["cpu"] = round(data.cpu_percent, 1) if data.cpu_percent is not None else None
        payload["mem"] = round(data.mem_percent, 1) if data.mem_percent is not None else None
        payload["if_total"] = len(data.interfaces)
        payload["if_up"] = sum(1 for iface in data.interfaces if iface.status == "up")

        # WLAN Controller: nhúng tổng AP online/offline để dashboard cập nhật thẻ
        # "Access Point" ngay sau khi AC poll xong, không phải chờ AJAX.
        if device.device_type == "wlan_controller":
            aps = data.extra.get("wifi_aps") if getattr(data, "extra", None) else None
            if isinstance(aps, list):
                ap_total = len(aps)
                ap_online = sum(1 for ap in aps if ap and ap.get("is_online"))
                payload["ap_total"] = ap_total
                payload["ap_online"] = ap_online
                payload["ap_offline"] = ap_total - ap_online
    return payload


def publish_device_event(device: "Device", online: bool, data: "NormalizedData | None" = None) -> None:
    """Phát event lên kênh riêng của thiết bị + kênh fleet. Không bao giờ raise."""
    try:
        body = json.dumps(build_payload(device, online, data), separators=(",", ":"))
        client = _get_client()
        client.publish(device_channel(device.pk), body)
        client.publish(FLEET_CHANNEL, body)
    except Exception as exc:  # noqa: BLE001 — publish phải im lặng tuyệt đối
        logger.warning("SSE publish failed for device %s: %s", getattr(device, "pk", "?"), exc)
