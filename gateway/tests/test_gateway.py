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
    """
    In-memory Redis mock for unit tests.

    Simulates the subset of Redis commands used by the gateway services
    (get, set, setex, incr, expire, exists, delete, keys, ttl, pipeline,
    script_load, evalsha) without requiring a real Redis instance.

    Scoped to the session so the same instance is shared across all tests,
    but the backing stores are cleared on fixture creation to avoid
    cross-test pollution.
    """
    store: dict = {}    # Simulates Redis key-value storage
    ttls:  dict = {}    # Tracks TTLs set via setex / expire
    scripts: dict = {}  # Holds loaded Lua scripts (script_load / evalsha)

    class FakeRedis:
        # --- Basic key operations ---

        async def ping(self):
            return True

        async def get(self, key):
            return store.get(key)

        async def set(self, key, value):
            store[key] = value
            return True

        async def setex(self, key, ttl, value):
            """Store a value with an associated TTL (seconds)."""
            store[key] = value
            ttls[key] = ttl
            return True

        async def incr(self, key):
            """Atomically increment an integer counter, defaulting from 0."""
            store[key] = str(int(store.get(key, "0")) + 1)
            return int(store[key])

        async def expire(self, key, ttl):
            """Set or update the TTL for an existing key."""
            ttls[key] = ttl
            return True

        async def exists(self, key):
            """Return 1 if the key exists, 0 otherwise (mirrors Redis behaviour)."""
            return 1 if key in store else 0

        async def delete(self, *keys):
            """Delete one or more keys; returns the number of keys removed."""
            for k in keys:
                store.pop(k, None)
            return len(keys)

        async def keys(self, pattern="*"):
            """
            Naive prefix-based key scan.
            Only supports trailing-wildcard patterns (e.g. 'rate:*').
            """
            return [k for k in store if k.startswith(pattern.replace("*", ""))]

        async def ttl(self, key):
            """Return the TTL for a key, or -1 if no TTL is set."""
            return ttls.get(key, -1)

        # --- Lua scripting (used by RateLimiter for atomic INCR + EXPIRE) ---

        async def script_load(self, script):
            """Register a Lua script and return a fake SHA digest."""
            scripts["loaded"] = script
            return "abc123"

        async def evalsha(self, sha, numkeys, *args):
            """
            Simulate the INCR + EXPIRE Lua script used by RateLimiter.
            Increments the counter for args[0] and returns the new value.
            TTL enforcement is omitted here since it is tested via setex.
            """
            key = args[0]
            store[key] = str(int(store.get(key, "0")) + 1)
            return int(store[key])

        # --- Pipeline (used for batching get + ttl in rate limiter reads) ---

        def pipeline(self):
            """
            Return a fake pipeline that queues get/ttl commands and
            executes them together, mirroring Redis pipeline behaviour.
            """
            class Pipe:
                def __init__(self):
                    self._cmds = []     # Queue of (command, key) tuples

                def get(self, key):
                    self._cmds.append(("get", key))
                    return self         # Return self for method chaining

                def ttl(self, key):
                    self._cmds.append(("ttl", key))
                    return self

                async def execute(self):
                    """Replay all queued commands against the in-memory store."""
                    results = []
                    for cmd, key in self._cmds:
                        if cmd == "get":
                            results.append(store.get(key))
                        elif cmd == "ttl":
                            results.append(ttls.get(key, -1))
                    return results

            return Pipe()

        async def aclose(self):
            """No-op: satisfies the async context manager close contract."""
            pass

    redis = FakeRedis()
    # Clear stores at fixture creation to prevent session-level state leakage
    store.clear()
    ttls.clear()
    return redis


# ══════════════════════════════════════════════════════════════════════════════
# Rate Limiter Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiter:
    """
    Unit tests for RateLimiter using the in-memory FakeRedis fixture.
    Each test uses a distinct IP address to avoid counter collisions.
    """

    @pytest.mark.asyncio
    async def test_allows_under_limit(self, mock_redis):
        """First request against a fresh IP should be allowed with count == 1."""
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        allowed, count, limit = await rl.check("1.2.3.4", route_limit=10)
        assert allowed
        assert count == 1

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self, mock_redis):
        """Request exceeding the route limit should be denied (allowed == False)."""
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "9.9.9.9"
        # Exhaust the limit with exactly route_limit requests
        for _ in range(5):
            await rl.check(ip, route_limit=5)
        # The (limit + 1)th request should be blocked
        allowed, count, limit = await rl.check(ip, route_limit=5)
        assert not allowed
        assert count == 6

    @pytest.mark.asyncio
    async def test_ban_and_detect(self, mock_redis):
        """Banning an IP should make is_banned() return True for that IP."""
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "10.0.0.1"
        assert not await rl.is_banned(ip)   # Should not be banned initially
        await rl.ban(ip, duration=60)        # Ban for 60 seconds
        assert await rl.is_banned(ip)        # Should now be banned

    @pytest.mark.asyncio
    async def test_reset_clears_counter(self, mock_redis):
        """After reset(), the next check should start the counter from 1 again."""
        from services.rate_limiter import RateLimiter
        rl = RateLimiter(mock_redis)
        ip = "5.5.5.5"
        for _ in range(3):
            await rl.check(ip, route_limit=100)
        await rl.reset(ip)
        allowed, count, _ = await rl.check(ip, route_limit=100)
        assert allowed
        assert count == 1   # Counter should have restarted from zero


# ══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """
    Unit tests for CircuitBreaker state transitions.
    Each test uses a unique service name to isolate Redis keys.

    State machine:
        CLOSED ──(failures >= threshold)──► OPEN
        OPEN   ──(recovery_timeout elapsed)─► HALF_OPEN
        HALF_OPEN ──(on_success)──► CLOSED
        HALF_OPEN ──(on_failure)──► OPEN
    """

    @pytest.mark.asyncio
    async def test_starts_closed(self, mock_redis):
        """A freshly created circuit breaker should be in the CLOSED state."""
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-1")
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_threshold(self, mock_redis):
        """Circuit should transition to OPEN once failures reach the configured threshold."""
        from services.circuit_breaker import CircuitBreaker, CircuitState
        from core.config import settings
        cb = CircuitBreaker(mock_redis, "test-service-2")
        threshold = settings.circuit_breaker.failure_threshold
        # Trigger exactly threshold failures to trip the breaker
        for _ in range(threshold):
            await cb.on_failure()
        state = await cb.get_state()
        assert state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_resets_on_success(self, mock_redis):
        """A successful call from HALF_OPEN state should close the circuit."""
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-3")
        # Manually place the breaker in HALF_OPEN (probe state after recovery timeout)
        await cb._set_state(CircuitState.HALF_OPEN)
        await cb.on_success()
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_raises_when_open(self, mock_redis):
        """before_call() should raise CircuitBreakerOpen when circuit is OPEN."""
        from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-4")
        await cb._set_state(CircuitState.OPEN)
        # Set opened_at to now so the recovery timeout has not elapsed yet
        await mock_redis.set(cb._key_opened, str(time.time()))
        with pytest.raises(CircuitBreakerOpen):
            await cb.before_call()

    @pytest.mark.asyncio
    async def test_reset_closes_circuit(self, mock_redis):
        """Calling reset() on an OPEN circuit should immediately close it."""
        from services.circuit_breaker import CircuitBreaker, CircuitState
        cb = CircuitBreaker(mock_redis, "test-service-5")
        await cb._set_state(CircuitState.OPEN)
        await cb.reset()
        state = await cb.get_state()
        assert state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_status_dict(self, mock_redis):
        """status() should return a dict containing at minimum state, failures, and threshold."""
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
    """
    Unit tests for ProxyService request forwarding and retry behaviour.
    httpx.AsyncClient.request is patched to avoid real network calls.
    asyncio.sleep is patched to make retry back-off instantaneous in tests.
    """

    @pytest.mark.asyncio
    async def test_forwards_200(self):
        """A successful downstream response should be forwarded with status 200."""
        from core.config import RouteConfig
        from services.proxy import ProxyService

        route = RouteConfig(path="/mock", target="http://localhost:8010")
        proxy = ProxyService(route=route)

        # Build a minimal mock request with all attributes ProxyService reads
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
        """
        ProxyService should retry on TimeoutException and succeed
        once the downstream eventually responds.
        Verifies that exactly 3 attempts are made (2 failures + 1 success).
        """
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
            """Fail with TimeoutException for the first 2 calls, succeed on the 3rd."""
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.TimeoutException("timeout")
            return HttpxResponse(200, content=b'{"ok":true}')

        with patch("httpx.AsyncClient.request", new=flaky_request):
            with patch("asyncio.sleep", new=AsyncMock()):   # Skip retry back-off delays
                resp = await proxy.forward(mock_request, "retry-req-id")

        assert call_count == 3          # Confirm all 3 attempts were made
        assert resp.status_code == 200  # Final attempt succeeded

    @pytest.mark.asyncio
    async def test_returns_502_after_all_retries_fail(self):
        """
        If every retry attempt times out, ProxyService should return a 502 Bad Gateway
        rather than raising an unhandled exception.
        """
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

        # side_effect on every call → all retries exhaust without success
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            with patch("asyncio.sleep", new=AsyncMock()):   # Skip back-off delays
                resp = await proxy.forward(mock_request, "fail-req-id")
        assert resp.status_code == 502


# ══════════════════════════════════════════════════════════════════════════════
# Auth Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    """Unit tests for JWT token creation and validation."""

    def test_create_and_decode_token(self):
        """A token created for a user should decode back to the correct subject and roles."""
        from services.auth import create_token, decode_token
        token = create_token("alice", roles=["admin"])
        claims = decode_token(token)
        assert claims["sub"] == "alice"         # Subject claim matches username
        assert "admin" in claims["roles"]       # Role claim is preserved

    def test_invalid_token_raises(self):
        """Decoding a malformed or tampered token should raise HTTP 401 Unauthorized."""
        from fastapi import HTTPException
        from services.auth import decode_token
        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.valid.token")
        assert exc_info.value.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Integration Smoke Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegration:
    """
    Light integration tests that boot the full FastAPI app via TestClient.
    Redis and downstream services are replaced with AsyncMocks via
    dependency_overrides so no external infrastructure is required.

    These tests verify that routing, middleware, and endpoint wiring are
    correct end-to-end, without testing individual service logic in depth
    (that is covered by the unit test classes above).
    """

    def setup_method(self):
        """
        Called automatically before each test method.
        Reserved for any per-test setup; currently a no-op since each test
        manages its own dependency overrides inline.
        """
        pass

    def test_health_endpoint(self):
        """
        GET /admin/health should return 200 when Redis is reachable.
        Redis is overridden with an AsyncMock whose ping() returns True.
        """
        from main import app
        from core.redis_client import get_redis

        async def override_redis():
            r = AsyncMock()
            r.ping  = AsyncMock(return_value=True)
            r.aclose = AsyncMock()
            return r

        app.dependency_overrides[get_redis] = override_redis
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/health")
            assert resp.status_code == 200
        # Always clear overrides after the test to avoid leaking state into other tests
        app.dependency_overrides.clear()

    def test_token_endpoint(self):
        """
        POST /auth/token with valid credentials should return a bearer token.
        No Redis override needed as this endpoint does not interact with Redis.
        """
        from main import app
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/auth/token", json={"username": "bob", "password": "secret"})
            assert resp.status_code == 200
            data = resp.json()
            assert "access_token" in data
            assert data["token_type"] == "bearer"

    def test_no_route_returns_404(self):
        """
        A request to an unregistered path should return 404 Not Found.
        Redis is overridden to simulate a clean state with no banned IPs
        and a rate limit counter of 1 (safely under any threshold).
        """
        from main import app
        from core.redis_client import get_redis

        async def override_redis():
            r = AsyncMock()
            r.exists      = AsyncMock(return_value=0)   # IP is not banned
            r.evalsha     = AsyncMock(return_value=1)   # First request, counter = 1
            r.script_load = AsyncMock(return_value="sha")
            r.get         = AsyncMock(return_value=None) # No circuit breaker state
            r.expire      = AsyncMock(return_value=True)
            return r

        app.dependency_overrides[get_redis] = override_redis
with TestClient(app, raise_server_exceptions=False) as client:
    resp = client.get("/nonexistent-path-xyz")
assert resp.status_code == 404
app.dependency_overrides.clear()
