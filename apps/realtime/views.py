"""Consumer phía web (ASGI): async SSE view subscribe Redis pub/sub rồi stream
event xuống browser. PHẢI chạy dưới ASGI server (uvicorn) — dưới sync WSGI mỗi
kết nối SSE sẽ chiếm trọn 1 worker.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import StreamingHttpResponse

from .channels import FLEET_CHANNEL, device_channel

# Khoảng phát heartbeat (giây). Đủ nhỏ để proxy/nginx không reap idle stream.
HEARTBEAT_TIMEOUT = 20


async def _event_stream(channel: str) -> AsyncGenerator[str, None]:
    client = aioredis.from_url(settings.REALTIME_REDIS_URL)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        yield ": connected\n\n"
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=HEARTBEAT_TIMEOUT,
            )
            if message and message.get("type") == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield f"event: metrics\ndata: {data}\n\n"
            else:
                # Hết timeout mà không có message → keepalive comment.
                yield ": heartbeat\n\n"
    finally:
        # Client đóng EventSource → generator bị close → dọn subscription tránh leak.
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()


def _sse_response(generator: AsyncGenerator[str, None]) -> StreamingHttpResponse:
    response = StreamingHttpResponse(generator, content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # tắt nginx buffering cho riêng response này
    return response


@login_required
async def fleet_stream(request) -> StreamingHttpResponse:
    """Stream event của TOÀN BỘ fleet — dùng cho dashboard index."""
    return _sse_response(_event_stream(FLEET_CHANNEL))


@login_required
async def device_stream(request, device_id: int) -> StreamingHttpResponse:
    """Stream event của 1 thiết bị — dùng cho trang chi tiết."""
    return _sse_response(_event_stream(device_channel(device_id)))
