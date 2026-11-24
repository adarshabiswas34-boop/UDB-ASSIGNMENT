"""
Integration tests for the rate limiter verifying:
(1) Requests under the limit returning 200 with accurate remaining headers
(2) Exceeding the limit returning 429 with Retry-After and JSON error payload
(3) Quota resetting or recovering after the time window passes
"""

import math
import time
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse
from src.middleware.rate_limit import RateLimitMiddleware
from src.limiter.rate_limiter import SlidingWindowLogLimiter, RateLimiter, RateLimitResult


class SimulatedRedisSlidingWindow:
    """
    A faithful in-memory simulation of the Redis Lua Sliding Window Log script.
    Maintains a simulated timestamp and sorted set for exact window evaluations.
    """

    def __init__(self):
        # Maps key -> list of (timestamp_ms, req_id)
        self.storage = {}
        self.current_time_ms = 1_700_000_000_000.0  # Simulated start epoch in ms

    def set_time_seconds(self, seconds: float):
        self.current_time_ms = seconds * 1000.0

    def advance_time_seconds(self, seconds: float):
        self.current_time_ms += seconds * 1000.0

    async def eval(self, script, numkeys, key, now_ms, window_ms, limit, request_id, cost, expire_seconds):
        now = float(self.current_time_ms)
        window = float(window_ms)
        lim = int(limit)
        c = int(cost)
        clear_before = now - window

        entries = self.storage.setdefault(key, [])

        # 1. Prune expired entries older than (now - window)
        entries = [entry for entry in entries if entry[0] > clear_before]
        self.storage[key] = entries

        current_count = len(entries)
        oldest_ts = entries[0][0] if entries else now
        reset_after = max(0.0, ((oldest_ts + window) - now) / 1000.0)

        # 2. Check quota
        if (current_count + c) <= lim:
            allowed = 1
            entries.append((now, request_id))
            remaining = lim - len(entries)
            retry_after = 0.0
        else:
            allowed = 0
            remaining = max(0, lim - current_count)
            retry_after = reset_after if reset_after > 0 else 0.001

        return [allowed, remaining, str(round(reset_after, 3)), str(round(retry_after, 3))]


@pytest.fixture
def simulated_redis():
    return SimulatedRedisSlidingWindow()


@pytest.fixture
def rate_limited_app(simulated_redis):
    app = FastAPI()

    # Create limiter backed by our simulated Redis
    backend = SlidingWindowLogLimiter(redis=simulated_redis)
    limiter = RateLimiter(redis=simulated_redis)
    limiter.backend = backend

    # Route limits: /api/test has limit=3 per 60 seconds
    route_limits = {
        "GET:/api/test": (3, 60),
    }

    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        route_limits=route_limits,
    )

    @app.get("/api/test")
    def test_endpoint():
        return {"status": "ok", "message": "Success"}

    return app, limiter, simulated_redis


def test_scenario_1_requests_under_limit_return_200_with_accurate_headers(rate_limited_app):
    """
    (1) Requests under the limit:
    Sends consecutive requests within quota and verifies:
    - HTTP 200 OK
    - X-RateLimit-Limit is accurate (3)
    - X-RateLimit-Remaining decrements accurately (2 -> 1 -> 0)
    - X-RateLimit-Reset is present and valid
    """
    app, limiter, sim_redis = rate_limited_app
    sim_redis.set_time_seconds(1_000.0)  # Start at t = 1000s

    with TestClient(app) as client:
        # Request 1: remaining should be 2
        r1 = client.get("/api/test", headers={"x-api-key": "client_alpha"})
        assert r1.status_code == 200
        assert r1.headers.get("X-RateLimit-Limit") == "3"
        assert r1.headers.get("X-RateLimit-Remaining") == "2"
        assert int(r1.headers.get("X-RateLimit-Reset")) > 0

        # Request 2: remaining should be 1
        sim_redis.advance_time_seconds(2.0)  # t = 1002s
        r2 = client.get("/api/test", headers={"x-api-key": "client_alpha"})
        assert r2.status_code == 200
        assert r2.headers.get("X-RateLimit-Limit") == "3"
        assert r2.headers.get("X-RateLimit-Remaining") == "1"

        # Request 3: remaining should be 0 (last permitted request in window)
        sim_redis.advance_time_seconds(3.0)  # t = 1005s
        r3 = client.get("/api/test", headers={"x-api-key": "client_alpha"})
        assert r3.status_code == 200
        assert r3.headers.get("X-RateLimit-Limit") == "3"
        assert r3.headers.get("X-RateLimit-Remaining") == "0"


def test_scenario_2_exceeding_limit_returns_429_with_retry_after(rate_limited_app):
    """
    (2) Exceeding the limit:
    Consumes all quota (3 requests), then verifies that the 4th request:
    - Returns HTTP 429 Too Many Requests
    - Includes Retry-After header with exact wait seconds
    - Includes X-RateLimit-Remaining: 0
    - Returns JSON payload with error details
    """
    app, limiter, sim_redis = rate_limited_app
    sim_redis.set_time_seconds(2_000.0)  # Start at t = 2000s

    with TestClient(app) as client:
        # Exhaust quota: 3 requests allowed
        for _ in range(3):
            res = client.get("/api/test", headers={"x-api-key": "client_beta"})
            assert res.status_code == 200

        # Request 4: limit exceeded!
        sim_redis.advance_time_seconds(1.0)  # t = 2001s
        r4 = client.get("/api/test", headers={"x-api-key": "client_beta"})

        assert r4.status_code == 429
        assert "Retry-After" in r4.headers
        retry_after = int(r4.headers.get("Retry-After"))
        assert retry_after > 0
        assert r4.headers.get("X-RateLimit-Limit") == "3"
        assert r4.headers.get("X-RateLimit-Remaining") == "0"

        # Verify JSON error payload
        body = r4.json()
        assert body["error"] == "Too Many Requests"
        assert f"Rate limit exceeded. Try again in {retry_after} seconds." in body["message"]
        assert body["retry_after"] == retry_after
        assert body["limit"] == 3
        assert body["remaining"] == 0


def test_scenario_3_quota_recovers_after_time_window_passes(rate_limited_app):
    """
    (3) Quota recovering after the time window passes:
    - Exhaust quota (3 requests at t=0s)
    - Verify request is rejected at t=10s (HTTP 429)
    - Advance time past the 60s sliding window to t=65s
    - Verify request is now allowed (HTTP 200) with quota recovered!
    """
    app, limiter, sim_redis = rate_limited_app
    sim_redis.set_time_seconds(3_000.0)  # Start at t = 3000s

    with TestClient(app) as client:
        # Send 3 requests at t = 3000s
        for _ in range(3):
            client.get("/api/test", headers={"x-api-key": "client_gamma"})

        # Immediate follow-up at t = 3010s: blocked (429)
        sim_redis.advance_time_seconds(10.0)  # t = 3010s
        blocked = client.get("/api/test", headers={"x-api-key": "client_gamma"})
        assert blocked.status_code == 429

        # Now advance time past the 60-second window: t = 3000 + 65 = 3065s
        # The 3 original requests (at t=3000s) are now 65s old (> 60s window) and are pruned
        sim_redis.set_time_seconds(3_065.0)

        recovered = client.get("/api/test", headers={"x-api-key": "client_gamma"})
        assert recovered.status_code == 200
        assert recovered.json() == {"status": "ok", "message": "Success"}
        assert recovered.headers.get("X-RateLimit-Limit") == "3"
        assert recovered.headers.get("X-RateLimit-Remaining") == "2"
