"""
Test suite for the API Gateway.

Covers unit tests for the rate limiter, circuit breaker, proxy/retry logic,
and JWT auth services, plus light integration smoke tests against the FastAPI app.

Run:
    pytest tests/ -v              # verbose output
    pytest tests/ -v --tb=short -x  # stop on first failure, short tracebacks
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient, Response as HttpxResponse


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def mock_redis():
    store: dict = {}
    ttls:  dict = {}
    scripts: dict = {}

    class FakeRedis:
        async def ping(self):
            return True

        async def get(self, key):
            return store.get(key)

        async def set(self, key, value):
            store[key] = value
            return True

        async def setex(self, key, ttl, value):
            store[key] = value
            ttls[key] = ttl
            return True

        async def incr(self, key):
            store[key] = str(int(store.get(key, "0")) + 1)
            return int(store[key])

        async def expire(self, key, ttl):
            ttls[key] = ttl
            return True

        async def exists(self, key):
            return 1 if key in store else 0

        async def delete(self, *keys):
            for k in keys:
                store.pop(k, None)
            return len(keys)

        async def keys(self, pattern="*"):
            return [k for k in store if k.startswith(pattern.replace("*", ""))]

        async def ttl(self, key):
            return ttls.get(key, -1)

        async def script_load(self, script):
            scripts["loaded"] = script
            return "abc123"

        async def evalsha(self, sha, numkeys, *args):
            key = args[0]
            store[key] = str(int(store.get(key, "0")) + 1)
            return int(store[key])

        def pipeline(self):
            class Pipe:
                def __init__(self):
                    self._cmds = []

                def get(self, key):
                    self._cmds.append(("get", key))
                    return self

                def ttl(self, key):
                    self._cmds.append(("ttl", key))
                    return self

                async def execute(self):
                    results = []
                    for cmd, key in self._cmds:
                        if cmd == "get":
                            results.append(store.get(key))
                        elif cmd == "ttl":
                            results.append(ttls.get(key, -1))
                    return results

            return Pipe()

        async def aclose(self):
            pass

    redis = FakeRedis()
    store.clear()
    ttls.clear()
    return redis


# ══════════════════════════════════════════════════════════════════════════════
# Rate Limiter Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiter:

    @pytest.mark.asyncio
    async def test_allows_under_limit(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        allowed, count, limit = await rl.check("1.2.3.4", route_limit=10)
        assert allowed
        assert count == 1

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "9.9.9.9"
        for _ in range(5):
            await rl.check(ip, route_limit=5)
        allowed, count, limit = await rl.check(ip, route_limit=5)
        assert not allowed
        assert count == 6

    @pytest.mark.asyncio
    async def test_ban_and_detect(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "10.0.0.1"
        assert not await rl.is_banned(ip)
        await rl.ban(ip, duration=60)
        assert await rl.is_banned(ip)

    @pytest.mark.asyncio
    async def test_reset_clears_counter(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "5.5.5.5"
        for _ in range(3):
            await rl.check(ip, route_limit=100)
        await rl.reset(ip)
        allowed, count, _ = await rl.check(ip, route_limit=100)
        assert allowed
        assert count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:

    @pytest.mark.asyncio
    async def test_starts_closed(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-1")
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        from core.config import settings
        cb = CircuitBreaker(mock_redis, "test-service-2")
        threshold = settings.circuit_breaker.failure_threshold
        for _ in range(threshold):
            await cb.on_failure()
        state = await cb.get_state()
        assert state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_resets_on_success(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-3")
        await cb._set_state(CircuitState.HALF_OPEN)
        await cb.on_success()
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_raises_when_open(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-4")
        await cb._set_state(CircuitState.OPEN)
        await mock_redis.set(cb._key_opened, str(time.time()))
        with pytest.raises(CircuitBreakerOpen):
            await cb.before_call()

    @pytest.mark.asyncio
    async def test_reset_closes_circuit(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-5")
        await cb._set_state(CircuitState.OPEN)
        await cb.reset()
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_status_dict(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker
        cb = CircuitBreaker(mock_redis, "test-service-6")
        status = await cb.status()
        assert "state"     in status
        assert "failures"  in status
        assert "threshold" in status


# ══════════════════════════════════════════════════════════════════════════════
# Proxy / Retry Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestProxy:

    @pytest.mark.asyncio
    async def test_forwards_200(self):
        from core.config import RouteConfig
        from services.proxy import ProxyService

        route = RouteConfig(path="/mock", target="http://localhost:8010")
        proxy = ProxyService(route=route)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/mock/echo"
        mock_request.url.query = ""
        mock_request.headers = {}
        mock_request.client.host = "127.0.0.1"
        mock_request.body = AsyncMock(return_value=b"")

        fake_response = HttpxResponse(
            200,
            content=b'{"ok":true}',
            headers={"content-type": "application/json"}
        )

        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=fake_response)):
            resp = await proxy.forward(mock_request, "test-req-id")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        import httpx
        from core.config import RouteConfig
        from services.proxy import ProxyService

        route = RouteConfig(path="/mock", target="http://localhost:8010")
        proxy = ProxyService(route=route)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/mock/echo"
        mock_request.url.query = ""
        mock_request.headers = {}
        mock_request.client.host = "127.0.0.1"
        mock_request.body = AsyncMock(return_value=b"")

        call_count = 0

        async def flaky_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            return HttpxResponse(200, content=b'{"ok":true}')

        with patch("httpx.AsyncClient.request", new=flaky_request):
            with patch("asyncio.sleep", new=AsyncMock()):
                resp = await proxy.forward(mock_request, "retry-req-id")

        assert call_count == 3
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_returns_502_after_all_retries_fail(self):
        import httpx
        from core.config import RouteConfig
        from services.proxy import ProxyService

        route = RouteConfig(path="/mock", target="http://localhost:8010")
        proxy = ProxyService(route=route)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/mock/echo"
        mock_request.url.query = ""
        mock_request.headers = {}
        mock_request.client.host = "127.0.0.1"
        mock_request.body = AsyncMock(return_value=b"")

        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            with patch("asyncio.sleep", new=AsyncMock()):
                resp = await proxy.forward(mock_request, "fail-req-id")
        assert resp.status_code == 502


# ══════════════════════════════════════════════════════════════════════════════
# Auth Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:

    def test_create_and_decode_token(self):
        from services.auth import create_token, decode_token
        token = create_token("alice", roles=["admin"])
        claims = decode_token(token)
        assert claims["sub"] == "alice"
        assert "admin" in claims["roles"]

    def test_invalid_token_raises(self):
        from fastapi import HTTPException
        from services.auth import decode_token
        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.valid.token")
        assert exc_info.value.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Integration Smoke Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:

    def setup_method(self):
        pass

    def test_health_endpoint(self):
        from main import app
        from core.redis_client import get_redis

        async def override_redis():
            r = AsyncMock()
            r.ping = AsyncMock(return_value=True)
            r.aclose = AsyncMock()
            return r

        app.dependency_overrides[get_redis] = override_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/health")
            assert resp.status_code == 200
        app.dependency_overrides.clear()

    def test_token_endpoint(self):
        from main import app
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/auth/token", json={"username": "bob", "password": "secret"})
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert data["token_type"] == "bearer"

    def test_no_route_returns_404(self):
        from main import app
        from core.redis_client import get_redis

        async def override_redis():
            r = AsyncMock()
            r.exists      = AsyncMock(return_value=0)
            r.evalsha     = AsyncMock(return_value=1)
            r.script_load = AsyncMock(return_value="sha")
            r.get         = AsyncMock(return_value=None)
            r.expire      = AsyncMock(return_value=True)
            return r

        app.dependency_overrides[get_redis] = override_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/nonexistent-path-xyz")
        assert resp.status_code == 404
        app.dependency_overrides.clear()
