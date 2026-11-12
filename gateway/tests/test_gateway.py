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
from urllib.parse import unquote

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


def _mock_request(method: str = "GET", raw_path: str = "/mock/echo", query: str = "", headers: dict | None = None):
    req = MagicMock()
    req.method = method
    req.scope = {"path": unquote(raw_path), "raw_path": raw_path.encode(), "query_string": query.encode()}
    req.headers = headers or {}
    req.client.host = "127.0.0.1"
    req.body = AsyncMock(return_value=b"")
    return req


def _starlette_request(path: str, authorization: str | None = None, cookie: str | None = None) -> StarletteRequest:
    headers = [(b"authorization", authorization.encode())] if authorization else []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
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
    async def test_route_without_a_limit_gets_the_default(self, mock_redis):
        # RouteConfig used to default rate_limit to 100, so default_limit never applied
        from core.config import RouteConfig, settings
        from services.rate_limiter import RateLimiter
        route = RouteConfig(path="/no-limit", target="http://t")
        assert route.rate_limit is None
        with patch.object(settings.rate_limiting, "default_limit", 7):
            _, _, limit = await RateLimiter(mock_redis).check("11.0.0.7", route.rate_limit, route.path)
        assert limit == 7

    @pytest.mark.asyncio
    async def test_retry_after_is_the_time_left_in_the_window(self, mock_redis):
        from core.config import settings
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        await rl.check("11.0.0.5", route_limit=1, route="/mock")
        await mock_redis.expire(rl._key("11.0.0.5", "/mock"), 17)  # the fake doesn't run the script's EXPIRE
        assert await rl.retry_after("11.0.0.5", "/mock") == 17
        # No counter (or Redis can't say): the whole window
        assert await rl.retry_after("11.0.0.6", "/mock") == settings.rate_limiting.window_seconds

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

        mock_request = _mock_request()

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

        mock_request = _mock_request()

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

        mock_request = _mock_request()

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

        mock_request = _mock_request(headers={"x-forwarded-for": "6.6.6.6"})

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
    async def test_keeps_the_encoding_header_of_a_body_httpx_could_not_decode(self):
        # An upstream that ignores Accept-Encoding and answers in br: httpx
        # passes the bytes through compressed, so the label must stay on them
        from core.config import RouteConfig
        from services.proxy import ProxyService

        compressed = b"\x8b\x02\x80not-really-brotli\x03"
        upstream = HttpxResponse(200, content=compressed, headers={"content-encoding": "br"})
        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=upstream)):
            resp = await proxy.forward(_mock_request(), "br")
        assert resp.body == compressed
        assert resp.headers["content-encoding"] == "br"
        assert resp.headers["content-length"] == str(len(compressed))

    @pytest.mark.asyncio
    async def test_forwards_the_path_as_sent(self):
        # Decoding "%2F" and "%3F" and re-encoding them would turn them into a
        # real path separator and a query delimiter upstream
        from core.config import RouteConfig
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010", strip_prefix=True))
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream):
            await proxy.forward(_mock_request(raw_path="/mock/a%2Fb%3Fc", query="x=%2F1"), "raw-path")
        assert str(downstream.call_args.kwargs["url"]) == "http://localhost:8010/a%2Fb%3Fc?x=%2F1"

    @pytest.mark.asyncio
    async def test_encoded_route_prefix_is_refused(self):
        # "/%6dock" decodes to "/mock" and matches the route, but stripping
        # five characters from the raw path would cut it in the wrong place
        from core.config import RouteConfig
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010", strip_prefix=True))
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream):
            resp = await proxy.forward(_mock_request(raw_path="/%6dock/echo"), "encoded-prefix")
        assert resp.status_code == 400
        downstream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_client_accept_encoding_is_not_forwarded(self):
        # httpx can't decode br here; an upstream that answered in it would
        # have its body passed on compressed with the header already removed
        import httpx
        from core.config import RouteConfig
        from services.proxy import ProxyService

        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream):
            await proxy.forward(_mock_request(headers={"accept-encoding": "br", "accept": "text/plain"}), "enc")
        sent = httpx.Headers(downstream.call_args.kwargs["headers"])
        assert "accept-encoding" not in sent
        assert sent["accept"] == "text/plain"

    @pytest.mark.asyncio
    async def test_repeated_upstream_headers_survive(self):
        # A dict of headers kept one Set-Cookie and joined the rest into it
        from core.config import RouteConfig
        from services.proxy import ProxyService

        upstream = HttpxResponse(200, content=b"{}", headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")])
        proxy = ProxyService(route=RouteConfig(path="/mock", target="http://localhost:8010"))
        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=upstream)):
            resp = await proxy.forward(_mock_request(), "cookies")
        assert resp.headers.getlist("set-cookie") == ["a=1", "b=2"]

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
        # A cookie identifies a caller as much as an Authorization header does
        anonymous = _cache_key(_starlette_request("/shop/cart"))
        with_session = _cache_key(_starlette_request("/shop/cart", cookie="session=alice"))
        assert anonymous != with_session

    @pytest.mark.asyncio
    async def test_binary_body_survives_the_cache(self, mock_redis):
        # Stored as text, a body that wasn't UTF-8 came back corrupted and
        # longer than its Content-Length
        from fastapi.responses import Response
        from services.cache import CacheService
        cache = CacheService(mock_redis)
        request = _starlette_request("/public/cache-test-binary")
        body = bytes(range(256))
        await cache.set(request, Response(content=body, media_type="image/png"))
        hit = await cache.get(request)
        assert hit.body == body
        assert hit.headers["content-length"] == str(len(body))
        assert hit.headers["content-type"] == "image/png"
        assert hit.headers["x-cache"] == "HIT"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("headers", [
        {"set-cookie": "session=abc"},
        {"cache-control": "private, max-age=60"},
        {"cache-control": "no-store"},
    ])
    async def test_responses_not_for_sharing_are_not_stored(self, mock_redis, headers):
        # One visitor's session cookie used to be replayed to the next
        from fastapi.responses import Response
        from services.cache import CacheService
        cache = CacheService(mock_redis)
        request = _starlette_request(f"/public/cache-test-{uuid.uuid4()}")
        await cache.set(request, Response(content=b"{}", headers=headers))
        assert await cache.get(request) is None

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
# Config Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConfig:

    def test_redis_password_comes_from_the_environment(self):
        # Under Kubernetes it lives in a Secret, not in the ConfigMap
        import os

        from core.config import load_settings
        with patch.dict(os.environ, {"REDIS_PASSWORD": "from-the-secret"}):
            assert load_settings().redis.password == "from-the-secret"

    def test_redis_password_survives_the_url(self):
        # Unquoted, the "@" would end the credentials and the rest become the host
        import redis.asyncio as aioredis
        from core.config import RedisConfig
        password = "p@ss:w/rd#1"
        url = RedisConfig(host="redis", password=password).url
        parsed = aioredis.ConnectionPool.from_url(url).connection_kwargs
        assert parsed["password"] == password
        assert parsed["host"] == "redis"

    def test_log_file_can_be_turned_off(self):
        # The Kubernetes ConfigMap sets logging.file to null: stdout only
        from core.config import Settings
        assert Settings(logging={"file": None}).logging.file is None

    def test_without_a_jwt_key_each_process_gets_a_random_one(self):
        # A key committed to the repository would let anyone sign tokens
        import os

        from core.config import load_settings
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JWT_SECRET_KEY", None)
            first, second = load_settings().jwt, load_settings().jwt
        assert first.key_was_generated and second.key_was_generated
        assert len(first.secret_key) == 64
        assert first.secret_key != second.secret_key

    def test_jwt_key_comes_from_the_environment(self):
        import os

        from core.config import load_settings
        with patch.dict(os.environ, {"JWT_SECRET_KEY": "k" * 40}):
            jwt_cfg = load_settings().jwt
        assert jwt_cfg.secret_key == "k" * 40
        assert not jwt_cfg.key_was_generated


# ══════════════════════════════════════════════════════════════════════════════
# Redis Client Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRedisClient:

    @pytest.mark.asyncio
    async def test_unreachable_redis_is_left_alone_for_the_cooldown(self):
        # A Redis that hangs made every command wait out its timeout, several
        # per request. After one failure the rest must fail without waiting.
        import redis.asyncio as aioredis
        from core.redis_client import FailFastRedis
        from redis.exceptions import TimeoutError as RedisTimeoutError

        client = FailFastRedis()  # connects only when a command runs, and none reaches the socket here
        real_call = AsyncMock(side_effect=[RedisTimeoutError("timed out"), "PONG"])
        with patch.object(aioredis.Redis, "execute_command", new=real_call):
            with pytest.raises(RedisTimeoutError):
                await client.ping()                 # the real attempt, which times out
            with pytest.raises(RedisConnectionError):
                await client.ping()                 # cooling down: Redis isn't asked
            assert real_call.await_count == 1
            assert client._down_until > time.monotonic()

            client._down_until = 0.0                # the cooldown has passed
            assert await client.ping() == "PONG"    # tried again, and it's back
        assert real_call.await_count == 2
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_redis_error_reply_does_not_start_the_cooldown(self):
        # Redis answered, so it is reachable; only the command was wrong
        import redis.asyncio as aioredis
        from core.redis_client import FailFastRedis
        from redis.exceptions import ResponseError

        client = FailFastRedis()
        with patch.object(aioredis.Redis, "execute_command", new=AsyncMock(side_effect=ResponseError("WRONGTYPE"))):
            with pytest.raises(ResponseError):
                await client.get("some-key")
        assert client._down_until == 0.0
        await client.aclose()


# ══════════════════════════════════════════════════════════════════════════════
# Integration Smoke Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:

    def setup_method(self):
        pass

    def test_probes_answer_200_when_redis_is_down(self):
        # Liveness never asks Redis. Readiness does, and reports it, but a
        # gateway that fails open is still ready: a 5xx here would pull every
        # pod out of rotation over an outage they can all serve through.
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = _down_redis
        with TestClient(app) as client:
            live = client.get("/admin/live")
            health = client.get("/admin/health")
        app.dependency_overrides.clear()

        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert health.json()["redis"] == "unreachable"

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

    def test_dot_segments_are_refused(self):
        # /users/../orders would be limited and authorised as /users, then
        # collapsed to /orders by the HTTP client on its way upstream
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: AsyncMock()
        downstream = AsyncMock(return_value=HttpxResponse(200, content=b"{}"))
        with patch("httpx.AsyncClient.request", new=downstream), TestClient(app) as client:
            resp = client.get("/users/%2e%2e/orders")
        app.dependency_overrides.clear()

        assert resp.status_code == 400
        downstream.assert_not_awaited()

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

    def test_cache_hit_keeps_the_gateway_headers(self, mock_redis):
        # A hit used to return early, without the rate-limit and timing headers
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        upstream = HttpxResponse(200, content=b'{"n":1}', headers={"content-type": "application/json"})
        downstream = AsyncMock(return_value=upstream)
        with patch("httpx.AsyncClient.request", new=downstream), TestClient(app) as client:
            miss = client.get("/mock/cache-headers")
            hit = client.get("/mock/cache-headers")
        app.dependency_overrides.clear()

        assert downstream.await_count == 1
        assert hit.headers["x-cache"] == "HIT"
        assert hit.json() == {"n": 1}
        assert "x-response-time-ms" in hit.headers
        assert hit.headers["x-request-id"] != miss.headers["x-request-id"]
        # ...and the hit was counted
        assert int(hit.headers["x-ratelimit-remaining"]) == int(miss.headers["x-ratelimit-remaining"]) - 1

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

    def test_ban_without_a_duration_uses_the_configured_one(self, mock_redis):
        # The endpoint's own default of 300 used to shadow ban_duration_seconds
        from core.config import settings
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        with patch.object(settings.rate_limiting, "ban_duration_seconds", 42), TestClient(app) as client:
            resp = client.post("/admin/rate-limit/12.0.0.2/ban")
        app.dependency_overrides.clear()

        assert resp.json()["duration_seconds"] == 42


    def test_admin_rate_limit_endpoints_take_ip_addresses_only(self, mock_redis):
        # A ban on "not-an-ip" used to be stored, and "::0001" would never match
        # the "::1" the limiter sees
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        with TestClient(app) as client:
            assert client.post("/admin/rate-limit/not-an-ip/ban").status_code == 400
            assert client.get("/admin/rate-limit/not-an-ip").json() == {"error": "Not an IP address: not-an-ip"}
            assert client.post("/admin/rate-limit/::0001/ban?duration=60").json()["ip"] == "::1"
            assert client.get("/admin/rate-limit/::1").json()["banned"] is True
            assert client.post("/admin/rate-limit/1.2.3.4/ban?duration=999999999").status_code == 422
        app.dependency_overrides.clear()

    def test_a_request_id_that_does_not_look_like_one_is_replaced(self):
        # It is logged on every line and sent upstream
        from main import app

        with TestClient(app) as client:
            kept = client.get("/admin/live", headers={"X-Request-ID": "abc-123.def:4"})
            too_long = client.get("/admin/live", headers={"X-Request-ID": "A" * 4000})
            odd = client.get("/admin/live", headers={"X-Request-ID": "x y"})
        assert kept.headers["x-request-id"] == "abc-123.def:4"
        for resp in (too_long, odd):
            assert len(resp.headers["x-request-id"]) == 36  # a fresh UUID

    def test_breakers_are_listed_once_per_target(self, mock_redis):
        # Three routes share mock-service, so they share one breaker
        from core.config import settings
        from core.redis_client import get_redis
        from main import app

        app.dependency_overrides[get_redis] = lambda: mock_redis
        with TestClient(app) as client:
            listed = client.get("/admin/circuit-breakers").json()
            unknown = client.post("/admin/circuit-breakers/http%3A%2F%2Fno-such-service/reset")
        app.dependency_overrides.clear()

        assert sorted(b["service"] for b in listed) == sorted({r.target for r in settings.routes})
        assert unknown.status_code == 404  # a reset writes a key; an unknown name would leave one behind

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
