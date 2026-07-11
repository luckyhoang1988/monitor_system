"""Redis scratch-state cho tính CPU% từ raw counter (delta 2 lần poll liên tiếp).

Dùng cho Synology DSM (`ssCpuIdle` không theo chuẩn UCD-SNMP-MIB — xem CLAUDE.md
mục "OID đã xác minh runtime"). Độc lập với `METRICS_WRITE_MODE`/`apps.metrics.cache`:
đây không phải dữ liệu metrics mà chỉ là bộ nhớ đệm bắt buộc để tính delta, nên luôn
chạy qua Redis bất kể chế độ ghi DB/cache. Dùng chung `CACHE_REDIS_URL` (Redis DB/1,
đã là dependency cứng của project) — khác key prefix nên không đụng `m:*` của
`apps.metrics.cache`.

Mất state (Redis restart/TTL hết) chỉ làm 1 poll thiếu baseline → cpu=0.0 tạm thời,
tự lành ở poll kế tiếp — không có rủi ro dữ liệu sai lệch kéo dài.
"""
from __future__ import annotations

import json
import logging

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_KEY = "cpu:raw:%s"
_TTL_SECS = 600  # vài chu kỳ poll — đủ sống qua 1 lần lỡ nhịp, không giữ mãi state cũ

_client: "redis.Redis | None" = None


def _get_client() -> "redis.Redis | None":
    global _client
    if _client is None:
        try:
            _client = redis.from_url(
                settings.CACHE_REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
        except Exception as exc:  # pragma: no cover - chỉ khi URL/redis-py hỏng
            logger.warning("cpu_state: không khởi tạo được redis client: %s", exc)
            return None
    return _client


def get_last_raw(device_id: int) -> "dict | None":
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(_KEY % device_id)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.debug("cpu_state get_last_raw(dev=%s) failed: %s", device_id, exc)
        return None


def set_last_raw(device_id: int, sample: dict) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.set(_KEY % device_id, json.dumps(sample), ex=_TTL_SECS)
    except Exception as exc:
        logger.debug("cpu_state set_last_raw(dev=%s) failed: %s", device_id, exc)
