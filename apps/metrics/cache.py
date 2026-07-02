"""Redis cache cho metrics — latest snapshot + ring-buffer time-series.

Giảm ghi Postgres: khi ``settings.METRICS_WRITE_MODE == "cache"`` metrics thường
xuyên nằm ở Redis DB /1 (tách Celery /0, realtime /2). Postgres CHỈ ghi khi có
sự cố (alert fire) hoặc đổi trạng thái quan trọng — xem writer + engine.

Thiết kế:
- ``m:latest:<device_id>``  STRING(JSON) — snapshot mới nhất (dashboard, prev-counter
  cho Mbps, alert "latest"). TTL = ``METRICS_LATEST_TTL_SECS``.
- ``m:series:sys:<device_id>``  LIST(JSON, newest-first) — mẫu ``{ts,cpu,mem}`` cho
  chart CPU/mem ngắn hạn + sustained alert. Cap ``METRICS_SERIES_MAX_SAMPLES``.
- ``m:series:if:<interface_id>``  LIST(JSON) — mẫu ``{ts,in_mbps,out_mbps,status}``.

Mọi thao tác nuốt exception: đọc lỗi → None/[]; ghi lỗi → trả False để caller tự
degrade sang ghi DB (không mất dữ liệu/cảnh báo khi Redis chết).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone as _tz

import redis
from django.conf import settings

logger = logging.getLogger(__name__)

_LATEST_KEY = "m:latest:%s"
_SYS_SERIES_KEY = "m:series:sys:%s"
_IF_SERIES_KEY = "m:series:if:%s"

_client: "redis.Redis | None" = None


def is_cache_mode() -> bool:
    """True nếu hệ thống đang ở chế độ cache-first (metrics vào Redis, không ghi raw)."""
    return getattr(settings, "METRICS_WRITE_MODE", "db") == "cache"


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
            logger.warning("metrics cache: không khởi tạo được redis client: %s", exc)
            return None
    return _client


def _ttl_latest() -> int:
    return int(getattr(settings, "METRICS_LATEST_TTL_SECS", 1800))


def _ttl_series() -> int:
    return int(getattr(settings, "METRICS_SERIES_TTL_SECS", 90000))


def _max_samples() -> int:
    return int(getattr(settings, "METRICS_SERIES_MAX_SAMPLES", 800))


def epoch_to_dt(ts: float) -> datetime:
    """Đổi epoch (giây, UTC) → datetime aware để dựng điểm chart."""
    return datetime.fromtimestamp(float(ts), tz=_tz.utc)


# ── Ghi ───────────────────────────────────────────────────────────────────────
def set_latest(device_id: int, snapshot: dict) -> bool:
    client = _get_client()
    if client is None:
        return False
    try:
        client.set(
            _LATEST_KEY % device_id,
            json.dumps(snapshot, default=str),
            ex=_ttl_latest(),
        )
        return True
    except Exception as exc:
        logger.warning("metrics cache set_latest(dev=%s) failed: %s", device_id, exc)
        return False


def push_series(
    device_id: int,
    sys_sample: dict | None,
    if_samples: "dict[int, dict] | None" = None,
) -> bool:
    """LPUSH mẫu mới vào ring-buffer sys + từng interface (1 pipeline), LTRIM + EXPIRE."""
    client = _get_client()
    if client is None:
        return False
    try:
        maxn = _max_samples()
        ttl = _ttl_series()
        pipe = client.pipeline(transaction=False)
        if sys_sample is not None:
            k = _SYS_SERIES_KEY % device_id
            pipe.lpush(k, json.dumps(sys_sample, default=str))
            pipe.ltrim(k, 0, maxn - 1)
            pipe.expire(k, ttl)
        for iid, sample in (if_samples or {}).items():
            k = _IF_SERIES_KEY % iid
            pipe.lpush(k, json.dumps(sample, default=str))
            pipe.ltrim(k, 0, maxn - 1)
            pipe.expire(k, ttl)
        pipe.execute()
        return True
    except Exception as exc:
        logger.warning("metrics cache push_series(dev=%s) failed: %s", device_id, exc)
        return False


# ── Đọc ───────────────────────────────────────────────────────────────────────
def get_latest(device_id: int) -> "dict | None":
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(_LATEST_KEY % device_id)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("metrics cache get_latest(dev=%s) failed: %s", device_id, exc)
        return None


def _get_series(key: str, since: "datetime | None") -> "list[dict]":
    """Trả mẫu trong buffer, sắp xếp TĂNG dần theo thời gian.

    Buffer là newest-first (LPUSH). Do đó khi gặp mẫu cũ hơn ``since`` là dừng
    (các mẫu sau đều cũ hơn) → không phải quét hết list.
    """
    client = _get_client()
    if client is None:
        return []
    try:
        raw = client.lrange(key, 0, -1)
    except Exception as exc:
        logger.warning("metrics cache lrange %s failed: %s", key, exc)
        return []
    cutoff = since.timestamp() if since else None
    out: list[dict] = []
    for item in raw:
        try:
            sample = json.loads(item)
        except (ValueError, TypeError):
            continue
        if cutoff is not None and sample.get("ts", 0) < cutoff:
            break
        out.append(sample)
    out.reverse()
    return out


def get_sys_series(device_id: int, since: "datetime | None" = None) -> "list[dict]":
    return _get_series(_SYS_SERIES_KEY % device_id, since)


def get_if_series(interface_id: int, since: "datetime | None" = None) -> "list[dict]":
    return _get_series(_IF_SERIES_KEY % interface_id, since)
