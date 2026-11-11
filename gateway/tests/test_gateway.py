"""
Test suite for the API Gateway.

Covers unit tests for the rate limiter, circuit breaker, proxy/retry logic,
cache keys and JWT auth services, plus light integration smoke tests against
the FastAPI app. Most tests fake Redis in memory; TestRealRedis runs the Lua
script and the breaker against a real Redis and is skipped when none is
reachable (CI provides one).

Run:
    pytest tests/ -v              # verbose output
    pytest tests/ -v --tb=short -x  # stop on first failure, short tracebacks
"""

import fnmatch
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.requests import Request as StarletteRequest


def _down_redis():
    """A Redis client whose every command fails, like during an outage."""
    async def fail(*args, **kwargs):
        raise RedisConnectionError("Redis is down")

    class DownPipeline:
        def __getattr__(self, name):
            # Queuing commands works; running them fails
            return fail if name == "execute" else (lambda *args, **kwargs: self)

    class DownRedis:
        def pipeline(self):
            return DownPipeline()

        def __getattr__(self, name):
            return fail

    return DownRedis()


def _mock_request(method: str = "GET"):
    req = MagicMock()
    req.method = method
    req.url.path = "/mock/echo"
    req.url.query = ""
    req.headers = {}
    req.client.host = "127.0.0.1"
    req.body = AsyncMock(return_value=b"")
    return req


def _starlette_request(path: str, authorization: str | None = None) -> StarletteRequest:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    return StarletteRequest({
        "type": "http", "method": "GET", "scheme": "http", "path": path,
        "query_string": b"", "headers": headers, "server": ("gateway", 8000),
    })

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
            # Like Redis: the number of keys that actually existed
            return sum(1 for k in keys if store.pop(k, None) is not None)

        async def keys(self, pattern="*"):
            return [k for k in store if k.startswith(pattern.replace("*", ""))]

        async def scan_iter(self, match="*", count=None):
            for k in list(store):
                if fnmatch.fnmatchcase(k, match):
                    yield k

        async def ttl(self, key):
            # Like Redis: -2 for a missing key, -1 for a key without expiry
            if key not in store:
                return -2
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

    @pytest.mark.asyncio
    async def test_limits_are_per_route(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "11.0.0.1"
        for _ in range(3):
            await rl.check(ip, route_limit=2, route="/mock")
        mock_allowed, _, _ = await rl.check(ip, route_limit=2, route="/mock")
        orders_allowed, orders_count, _ = await rl.check(ip, route_limit=2, route="/orders")
        assert not mock_allowed
        # Traffic on /mock doesn't use up the /orders limit
        assert orders_allowed
        assert orders_count == 1

    @pytest.mark.asyncio
    async def test_reset_clears_every_route(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "11.0.0.2"
        await rl.check(ip, route_limit=100, route="/users")
        await rl.check(ip, route_limit=100, route="/orders")
        await rl.reset(ip)
        _, users_count, _ = await rl.check(ip, route_limit=100, route="/users")
        _, orders_count, _ = await rl.check(ip, route_limit=100, route="/orders")
        assert users_count == 1
        assert orders_count == 1

    @pytest.mark.asyncio
    async def test_unban(self, mock_redis):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "11.0.0.3"
        await rl.ban(ip, duration=60)
        assert await rl.unban(ip)
        assert not await rl.is_banned(ip)
        assert not await rl.unban(ip)  # nothing left to lift

    @pytest.mark.asyncio
    async def test_fails_open_when_redis_is_down(self):
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(_down_redis())
        assert not await rl.is_banned("11.0.0.4")
        allowed, _, _ = await rl.check("11.0.0.4", route_limit=1, route="/mock")
        assert allowed


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
        from core.config import settings
        from services.circuit_breaker import CircuitBreaker, CircuitState
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
        from services.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerOpen,
            CircuitState,
        )
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
    async def test_open_moves_to_half_open_after_timeout(self, mock_redis):
        from core.config import settings
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-7")
        await cb._set_state(CircuitState.OPEN)
        opened = time.time() - settings.circuit_breaker.recovery_timeout_seconds - 1
        await mock_redis.set(cb._key_opened, str(opened))
        assert await cb.before_call() == CircuitState.HALF_OPEN
        assert await cb.get_state() == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_recovers_when_open_timestamp_is_missing(self, mock_redis):
        # Regression: the timestamp used to expire after 4x the recovery
        # timeout, leaving the circuit OPEN forever after a quiet period
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-8")
        await cb._set_state(CircuitState.OPEN)
        assert await cb.before_call() == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_failed_probe_reopens(self, mock_redis):
        from services.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerOpen,
            CircuitState,
        )
        cb = CircuitBreaker(mock_redis, "test-service-9")
        await cb._set_state(CircuitState.HALF_OPEN)
        await cb.on_failure(CircuitState.HALF_OPEN)
        assert await cb.get_state() == CircuitState.OPEN
        with pytest.raises(CircuitBreakerOpen):  # the recovery timer restarted
            await cb.before_call()

    @pytest.mark.asyncio
    async def test_half_open_allows_limited_probes(self, mock_redis):
        from core.config import settings
        from services.circuit_breaker import (
            CircuitBreaker,
            CircuitBreakerOpen,
            CircuitState,
        )
        cb = CircuitBreaker(mock_redis, "test-service-10")
        await cb._set_state(CircuitState.HALF_OPEN)
        for _ in range(settings.circuit_breaker.half_open_max_calls):
            assert await cb.before_call() == CircuitState.HALF_OPEN
        with pytest.raises(CircuitBreakerOpen):
            await cb.before_call()

    @pytest.mark.asyncio
    async def test_success_when_closed_writes_no_state(self, mock_redis):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-11")
        await cb.on_failure(CircuitState.CLOSED)
        await cb.on_success(CircuitState.CLOSED)
        assert await mock_redis.get(cb._key_state) is None  # no state write on the hot path
        assert await cb._failures() == 0                    # failures must be consecutive

    @pytest.mark.asyncio
    async def test_breaker_fails_open_when_redis_is_down(self):
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(_down_redis(), "test-service-12")
        assert await cb.before_call() == CircuitState.CLOSED
        await cb.on_failure(CircuitState.CLOSED)  # must not raise

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

        with patch("httpx.AsyncClient.request", new=flaky_request), patch("asyncio.sleep", new=AsyncMock()):
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

    @pytest.mark.asyncio
    async def test_replaces_client_forwarded_for(self):
        import httpx
        from core.config import RouteConfig
        from services.proxy import ProxyService

        route = RouteConfig(path="/mock", target="http://localhost:8010")
        proxy = ProxyService(route=route)

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/mock/echo"
        mock_request.url.query = ""
        mock_request.headers = {"x-forwarded-for": "6.6.6.6"}
        mock_request.client.host = "127.0.0.1"
        mock_request.body = AsyncMock(return_value=b"")

        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream):
            await proxy.forward(mock_request, "xff-req-id")

        sent = httpx.Headers(downstream.call_args.kwargs["headers"])
        assert sent.get_list("x-forwarded-for") == ["127.0.0.1"]

    @pytest.mark.asyncio
    async def test_post_not_retried_after_timeout(self):
        # The upstream may have processed it; a retry could create it twice
        import httpx
        from core.config import RouteConfig
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        downstream = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        with patch("httpx.AsyncClient.request", new=downstream), patch("asyncio.sleep", new=AsyncMock()):
            resp = await proxy.forward(_mock_request("POST"), "post-timeout")
        assert resp.status_code == 502
        assert downstream.await_count == 1

    @pytest.mark.asyncio
    async def test_post_not_retried_on_503(self):
        from core.config import RouteConfig
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        downstream = AsyncMock(return_value=HttpxResponse(503, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream), patch("asyncio.sleep", new=AsyncMock()):
            resp = await proxy.forward(_mock_request("POST"), "post-503")
        assert resp.status_code == 503
        assert downstream.await_count == 1

    @pytest.mark.asyncio
    async def test_post_retried_when_connection_never_opened(self):
        # A refused connection means the upstream never saw the request
        import httpx
        from core.config import RouteConfig, settings
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        downstream = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with patch("httpx.AsyncClient.request", new=downstream), patch("asyncio.sleep", new=AsyncMock()):
            resp = await proxy.forward(_mock_request("POST"), "post-refused")
        assert resp.status_code == 502
        assert downstream.await_count == settings.retry.max_attempts

    @pytest.mark.asyncio
    async def test_retries_count_as_one_breaker_failure(self, mock_redis):
        from core.config import RouteConfig, settings
        from services.circuit_breaker import CircuitBreaker
        from services.proxy import ProxyService

        cb = CircuitBreaker(mock_redis, "test-service-13")
        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"), circuit_breaker=cb)
        downstream = AsyncMock(return_value=HttpxResponse(503, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream), patch("asyncio.sleep", new=AsyncMock()):
            await proxy.forward(_mock_request("GET"), "one-failure")
        assert downstream.await_count == settings.retry.max_attempts  # the GET was retried...
        assert await cb._failures() == 1                               # ...but counts once

    @pytest.mark.asyncio
    async def test_drops_encoding_headers_of_decoded_body(self):
        # httpx decompresses the body, so "gzip" would no longer describe it
        import gzip

        from core.config import RouteConfig
        from services.proxy import ProxyService

        body = b'{"ok":true}'
        upstream = HttpxResponse(
            200,
            content=gzip.compress(body),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=upstream)):
            resp = await proxy.forward(_mock_request(), "gzip")
        assert resp.body == body
        assert "content-encoding" not in resp.headers
        assert resp.headers["content-length"] == str(len(body))

    @pytest.mark.asyncio
    async def test_open_circuit_response_hides_the_target(self, mock_redis):
        # The target is an internal hostname; it belongs in the log, not the body
        from core.config import RouteConfig
        from services.circuit_breaker import CircuitBreaker, CircuitState
        from services.proxy import ProxyService

        target = "http://internal-host:9999"
        cb = CircuitBreaker(mock_redis, target)
        await cb._set_state(CircuitState.OPEN)
        await mock_redis.set(cb._key_opened, str(time.time()))
        proxy = ProxyService(route=RouteConfig(path="/mock", target=target), circuit_breaker=cb)
        resp = await proxy.forward(_mock_request(), "open-circuit")
        assert resp.status_code == 503
        assert b"internal-host" not in resp.body


# ══════════════════════════════════════════════════════════════════════════════
# Cache Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCache:

    def test_key_separates_callers(self):
        from services.cache import _cache_key
        alice = _cache_key(_starlette_request("/users/me", "Bearer alice-token"))
        bob = _cache_key(_starlette_request("/users/me", "Bearer bob-token"))
        alice_again = _cache_key(_starlette_request("/users/me", "Bearer alice-token"))
        assert alice != bob
        assert alice == alice_again

    @pytest.mark.asyncio
    async def test_invalidate_by_path_pattern(self, mock_redis):
        from services.cache import CacheService, _cache_key
        users = _cache_key(_starlette_request("/users/cache-test-1"))
        orders = _cache_key(_starlette_request("/orders/cache-test-1"))
        await mock_redis.setex(users, 60, "{}")
        await mock_redis.setex(orders, 60, "{}")
        assert await CacheService(mock_redis).invalidate("/users/cache-test*") == 1
        assert await mock_redis.get(users) is None
        assert await mock_redis.get(orders) is not None


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
        assert exc_info.value.detail == "Invalid token"  # the reason stays in the log

    def test_token_without_expiry_is_rejected(self):
        # PyJWT only checks exp when the claim is present, so it has to be required
        import jwt
        from core.config import settings
        from fastapi import HTTPException
        from services.auth import decode_token
        token = jwt.encode({"sub": "alice"}, settings.jwt.secret_key, algorithm=settings.jwt.algorithm)
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Integration Smoke Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:

    def setup_method(self):
        pass

    def test_health_endpoint(self):
        from core.redis_client import get_redis
        from main import app

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
        from core.redis_client import get_redis
        from main import app

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
            quoted = client.get("/a%22b")
        assert resp.status_code == 404
        # The path goes through the JSON encoder: a quote in it used to break the body
        assert quoted.json() == {"error": 'No route configured for /a"b'}
        app.dependency_overrides.clear()

    def test_unhandled_error_keeps_the_request_id(self):
        # Starlette's own 500 handler runs outside the logging middleware and
        # used to answer without the ID the log line carries
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        with patch("routers.proxy._match_route", side_effect=RuntimeError("boom")), TestClient(app) as client:
            resp = client.get("/mock/users", headers={"X-Request-ID": "trace-500"})
        app.dependency_overrides.clear()

        assert resp.status_code == 500
        assert resp.json() == {"error": "Internal gateway error"}
        assert resp.headers["x-request-id"] == "trace-500"

    def test_rate_limiter_ignores_spoofed_forwarded_for(self):
        from core.redis_client import get_redis
        from main import app
        from services.rate_limiter import RateLimiter

        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        is_banned = AsyncMock(return_value=True)  # short-circuits before cache and proxy
        with patch.object(RateLimiter, "is_banned", new=is_banned), TestClient(app) as client:
            resp = client.get("/mock/users", headers={"X-Forwarded-For": "6.6.6.6"})
        app.dependency_overrides.clear()

        assert resp.status_code == 429
        is_banned.assert_awaited_once_with("testclient")  # the TestClient peer, not the header

    def test_request_id_matches_downstream(self, mock_redis):
        import httpx
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream), TestClient(app) as client:
            supplied = client.get("/mock/trace-supplied", headers={"X-Request-ID": "trace-abc"})
            sent_supplied = httpx.Headers(downstream.call_args.kwargs["headers"])
            generated = client.get("/mock/trace-generated")
            sent_generated = httpx.Headers(downstream.call_args.kwargs["headers"])
        app.dependency_overrides.clear()

        # A client-supplied ID is kept end-to-end, and replaces the header rather than joining it
        assert sent_supplied.get_list("x-request-id") == ["trace-abc"]
        assert supplied.headers["x-request-id"] == "trace-abc"
        # A generated ID is the same one the client gets back
        assert sent_generated.get_list("x-request-id") == [generated.headers["x-request-id"]]

    def test_metrics_exposes_http_and_breaker_metrics(self):
        from core.redis_client import get_redis
        from main import app

        states = {"cb:http://mock-service:8010:state": "OPEN", "cb:http://mock-service:8010:failures": "5"}

        async def override_redis():
            r = AsyncMock()
            r.get = AsyncMock(side_effect=lambda key: states.get(key))
            return r

        app.dependency_overrides[get_redis] = override_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            client.post("/auth/token", json={"username": "bob", "password": "secret"})
            resp = client.get("/metrics")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.text
        assert 'gateway_circuit_breaker_state{route="/mock",target="http://mock-service:8010"} 2.0' in body
        assert 'gateway_circuit_breaker_state{route="/public",target="http://public-service:8003"} 0.0' in body
        assert 'gateway_circuit_breaker_failures{route="/mock",target="http://mock-service:8010"} 5.0' in body
        assert "gateway_redis_up 1.0" in body
        assert 'http_requests_total{handler="/auth/token",method="POST",status="2xx"}' in body
        assert 'handler="/metrics"' not in body  # scrapes are excluded from HTTP metrics

    def test_metrics_survives_redis_outage(self):
        from core.redis_client import get_redis
        from main import app

        async def override_redis():
            r = AsyncMock()
            r.get = AsyncMock(side_effect=ConnectionError("redis down"))
            return r

        app.dependency_overrides[get_redis] = override_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/metrics")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert "gateway_redis_up 0.0" in resp.text

    def test_proxy_keeps_serving_when_redis_is_down(self):
        # Limits, bans, cache and breaker all fail open
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = _down_redis
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b'{"ok":true}'))
        with patch("httpx.AsyncClient.request", new=downstream), TestClient(app) as client:
            resp = client.get("/mock/users")
        app.dependency_overrides.clear()

        assert resp.status_code == 200

    def test_admin_errors_hide_redis_details(self):
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = _down_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/rate-limit/12.0.0.9")
        app.dependency_overrides.clear()

        assert resp.status_code == 503
        assert resp.json() == {"error": "Gateway state store unavailable"}

    def test_unban_endpoint(self, mock_redis):
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        with TestClient(app) as client:
            client.post("/admin/rate-limit/12.0.0.1/ban?duration=600")
            assert client.get("/admin/rate-limit/12.0.0.1").json()["banned"] is True
            assert client.delete("/admin/rate-limit/12.0.0.1/ban").json()["status"] == "unbanned"
            assert client.get("/admin/rate-limit/12.0.0.1").json()["banned"] is False
        app.dependency_overrides.clear()

    def test_reset_one_breaker_by_target(self, mock_redis):
        # The target contains slashes; this endpoint used to fall through to a 404
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        with TestClient(app) as client:
            resp = client.post("/admin/circuit-breakers/http%3A%2F%2Fmock-service%3A8010/reset")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        assert resp.json()["service"] == "http://mock-service:8010"


# ══════════════════════════════════════════════════════════════════════════════
# Real Redis (skipped unless one is reachable; CI runs a service container)
# ══════════════════════════════════════════════════════════════════════════════

def _real_redis_url() -> str | None:
    import redis
    from core.config import settings
    try:
        redis.Redis.from_url(settings.redis.url, socket_connect_timeout=0.5).ping()
    except Exception:
        return None
    return settings.redis.url


_REAL_REDIS_URL = _real_redis_url()


@pytest.mark.skipif(_REAL_REDIS_URL is None, reason="no Redis reachable (CI provides one)")
class TestRealRedis:
    """The fake can't run Lua or expire keys; these check the real behaviour."""

    @pytest.mark.asyncio
    async def test_lua_counter_has_a_ttl_and_is_per_route(self):
        import redis.asyncio as aioredis
        from core.config import settings
        from services.rate_limiter import RateLimiter

        r = aioredis.from_url(_REAL_REDIS_URL, decode_responses=True)
        rl = RateLimiter(r)
        ip = f"test-{uuid.uuid4()}"
        try:
            await rl.check(ip, route_limit=5, route="/users")
            _, count, _ = await rl.check(ip, route_limit=5, route="/users")
            assert count == 2
            assert 0 < await r.ttl(f"rl:{ip}:/users") <= settings.rate_limiting.window_seconds
            assert await r.get(f"rl:{ip}:/orders") is None
        finally:
            await rl.reset(ip)
            await r.aclose()

    @pytest.mark.asyncio
    async def test_breaker_recovers_after_a_quiet_period(self):
        import redis.asyncio as aioredis
        from core.config import settings
        from services.circuit_breaker import CircuitBreaker, CircuitState

        r = aioredis.from_url(_REAL_REDIS_URL, decode_responses=True)
        cb = CircuitBreaker(r, f"http://test-{uuid.uuid4()}")
        cfg = settings.circuit_breaker
        try:
            for _ in range(cfg.failure_threshold):
                await cb.on_failure(CircuitState.CLOSED)
            assert await cb.get_state() == CircuitState.OPEN
            assert await r.ttl(cb._key_opened) == -1  # stored without an expiry
            # The recovery timeout passes with no traffic at all
            await r.set(cb._key_opened, str(time.time() - cfg.recovery_timeout_seconds - 1))
            assert await cb.before_call() == CircuitState.HALF_OPEN
            await cb.on_success(CircuitState.HALF_OPEN)
            assert await cb.get_state() == CircuitState.CLOSED
        finally:
            await r.delete(cb._key_state, cb._key_failures, cb._key_opened, cb._key_probes)
            await r.aclose()
