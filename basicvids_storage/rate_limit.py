import os
from time import monotonic

import redis.asyncio as redis
from fastapi import HTTPException, Request

from basicvids_storage.settings import settings


_redis_client: redis.Redis | None = None
_redis_unavailable_until = 0.0


def get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
        )
    return _redis_client


def client_identifier(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


async def enforce_rate_limit(name: str, identifier: str, limit: int, window_seconds: int) -> None:
    global _redis_unavailable_until
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    if monotonic() < _redis_unavailable_until:
        return

    key = f"rate_limit:storage:{name}:{identifier}"
    client = get_redis_client()

    try:
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
        if count > limit:
            ttl = await client.ttl(key)
            headers = {"Retry-After": str(max(ttl, 1))} if ttl > 0 else None
            raise HTTPException(status_code=429, detail="Too many requests", headers=headers)
    except HTTPException:
        raise
    except redis.RedisError:
        _redis_unavailable_until = monotonic() + 30
        return
