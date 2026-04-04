#  API Gateway ⚡

A high-performance, production ready API Gateway built with **FastAPI**, **httpx**, and **Redis**.

![CI](https://github.com/DimitriosDalaklidhs/api-gateway/actions/workflows/main.yml/badge.svg)
![CD](https://github.com/DimitriosDalaklidhs/api-gateway/actions/workflows/cd.yml/badge.svg)

```
 Clients
    │
    ▼
┌─────────────────────────────────────────┐
│            API Gateway :8000            │
│  ┌──────────┐  ┌──────────────────────┐ │
│  │ Logging  │  │   Rate Limiter       │ │
│  │Middleware│  │ (Redis sliding-win)  │ │
│  └──────────┘  └──────────────────────┘ │
│  ┌──────────┐  ┌──────────────────────┐ │
│  │  Circuit │  │   Proxy + Retry      │ │
│  │ Breaker  │  │ (httpx, backoff)     │ │
│  └──────────┘  └──────────────────────┘ │
│  ┌──────────┐  ┌──────────────────────┐ │
│  │   Cache  │  │    JWT Auth          │ │
│  │ (Redis)  │  │                      │ │
│  └──────────┘  └──────────────────────┘ │
└─────────────────────────────────────────┘
    │              │              │
    ▼              ▼              ▼
user-service   order-service  other-service
  :8001           :8002
```

---

## Features

| Phase | Feature | Details |
|-------|---------|---------|
| 1 | **Dynamic Proxy** | Routes by path prefix; supports GET/POST/PUT/DELETE/PATCH |
| 2 | **Logging Middleware** | JSON-structured logs to stdout + rotating file |
| 3 | **Rate Limiting** | Redis sliding-window counter; per-IP, per-route limits; temp bans |
| 4 | **Retries** | Exponential backoff; configurable retry-on status codes |
| 5 | **Circuit Breaker** | CLOSED → OPEN → HALF-OPEN; shared state via Redis |
| 6 | **Config System** | YAML file + environment variable overrides |
| ★ | **JWT Auth** | Bearer token validation; optional per-route |
| ★ | **Response Cache** | Redis GET cache; TTL configurable; admin invalidation |
| ★ | **Admin API** | `/admin/*` control plane for live inspection & control |
| ★ | **Mock Service** | Built-in downstream simulator with failure injection |

---

## Project Structure

```
gateway/
├── main.py                    # FastAPI app, lifespan, middleware wiring
├── config.yaml                # Route table + all tunable settings
├── mock_service.py            # Fake downstream (users/orders/echo/slow)
├── requirements.txt
├── Dockerfile
├── Dockerfile.mock
├── docker-compose.yml
│
├── core/
│   ├── config.py              # Pydantic settings loader (YAML + env vars)
│   ├── logging.py             # JSON formatter + rotating file handler
│   └── redis_client.py        # Shared async Redis pool
│
├── services/
│   ├── proxy.py               # Phase 1+4: httpx proxy + retry logic
│   ├── rate_limiter.py        # Phase 3: Redis INCR sliding-window
│   ├── circuit_breaker.py     # Phase 5: CLOSED/OPEN/HALF-OPEN FSM
│   ├── cache.py               # Bonus: Redis GET cache
│   └── auth.py                # Bonus: JWT create/decode/dependency
│
├── routers/
│   ├── proxy.py               # Catch-all /{path} → route match + forward
│   └── admin.py               # /admin/* + /auth/token endpoints
│
├── models/
│   └── schemas.py             # Pydantic request/response models
│
├── utils/
│   └── middleware.py          # Phase 2: LoggingMiddleware (ASGI)
│
└── tests/
    └── test_gateway.py        # 18 unit + integration tests (all passing)
```

---

## Prerequisites

- **Docker & Docker Compose** : for running the full stack
- **Python 3.11+** : for local development
- **Redis 7+** : provided via Docker Compose, or run separately

---

## Quick Start

### Option A : Docker Compose (recommended)

```bash
docker compose up --build
```

Services:
- Gateway → http://localhost:8000
- Mock service → http://localhost:8010
- Redis → localhost:6379

### Option B : Local dev

```bash
# 1. Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# 2. Install deps
pip install -r requirements.txt

# 3. Start mock downstream
uvicorn mock_service:app --port 8010 &

# 4. Start gateway
cd gateway && uvicorn main:app --port 8000 --reload
```

---

## Configuration (`config.yaml`)

### Adding a route

```yaml
routes:
  - path: "/payments"          # matched by prefix
    target: "http://pay-service:8005"
    rate_limit: 30             # req/min per IP (overrides default)
    strip_prefix: false        # keep /payments in forwarded URL
    methods: ["GET", "POST"]
    auth_required: true        # require Bearer JWT
```

### Tuning rate limits

```yaml
rate_limiting:
  enabled: true
  default_limit: 100           # requests per window
  window_seconds: 60
  ban_duration_seconds: 300    # how long to block an IP after manual ban
```

### Tuning the circuit breaker

```yaml
circuit_breaker:
  failure_threshold: 5         # consecutive failures before OPEN
  recovery_timeout_seconds: 30 # time in OPEN before trying HALF-OPEN
  half_open_max_calls: 3       # probe calls allowed in HALF-OPEN
```

### Environment variable overrides

| Variable | Description |
|----------|-------------|
| `REDIS_HOST` | Redis hostname |
| `REDIS_PORT` | Redis port |
| `JWT_SECRET_KEY` | Secret for JWT signing |

---

## API Reference

### Authentication

```bash
# Get a JWT
curl -X POST http://localhost:8000/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "any"}'

# Use it
curl http://localhost:8000/users \
  -H "Authorization: Bearer <token>"
```

### Proxy requests (via mock service)

```bash
# List users (auth-gated)
curl http://localhost:8000/mock/users

# Echo any request
curl -X POST http://localhost:8000/mock/echo \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'

# Trigger a slow response (tests retry/timeout)
curl "http://localhost:8000/mock/slow?delay=3"

# Force a 500 (tests circuit breaker)
curl http://localhost:8000/mock/status/500
```

### Admin endpoints

```bash
# Health check
curl http://localhost:8000/admin/health

# Live metrics (routes, circuit states, config)
curl http://localhost:8000/admin/metrics

# Circuit breaker states
curl http://localhost:8000/admin/circuit-breakers

# Reset all circuits
curl -X POST http://localhost:8000/admin/circuit-breakers/reset

# Rate-limit stats for an IP
curl http://localhost:8000/admin/rate-limit/1.2.3.4

# Manually ban an IP for 10 minutes
curl -X POST "http://localhost:8000/admin/rate-limit/1.2.3.4/ban?duration=600"

# Invalidate cache
curl -X POST "http://localhost:8000/admin/cache/invalidate?pattern=*"
```

### Failure injection (mock service)

```bash
# Make 70% of mock requests fail with 503
curl -X POST "http://localhost:8010/mock/failure-mode?enabled=true&rate=0.7&status=503"

# Watch the circuit breaker open after 5 failures
watch -n1 'curl -s http://localhost:8000/admin/circuit-breakers | python3 -m json.tool'

# Restore normal operation
curl -X POST "http://localhost:8010/mock/failure-mode?enabled=false"
```

---

## Response Headers

Every proxied response includes:

| Header | Description |
|--------|-------------|
| `X-Request-ID` | UUID per request; traceable end-to-end |
| `X-RateLimit-Limit` | Effective limit for this route |
| `X-RateLimit-Remaining` | Requests left in the current window |
| `X-Response-Time-Ms` | Total gateway latency in milliseconds |
| `X-Cache` | `HIT` when served from Redis cache |

---

## Running Tests

Tests use an in memory Redis mock, no external infrastructure required.

```bash
cd gateway
PYTHONPATH=. pytest tests/ -v --asyncio-mode=auto
```

```
18 passed in 2.11s
```

Test coverage:
- Rate limiter: allow, block, ban, reset
- Circuit breaker: all 3 state transitions
- Proxy: 200 forward, timeout retry, 502 exhaustion
- Auth: token create/decode, invalid token rejection
- Integration: health, token endpoint, 404 for unknown routes

---

## CI/CD

This project uses **GitHub Actions** for a full CI/CD pipeline.

### CI : Continuous Integration

Triggers on every push to `main` or `dev`, and on every pull request. Completes in under 25 seconds.

1. Spins up a **Redis 7** service container
2. Installs all dependencies
3. Lints with **ruff**
4. Runs all 18 tests with **pytest**

Workflow: [`.github/workflows/main.yml`](.github/workflows/main.yml)

### CD : Continuous Deployment

Triggers automatically after CI passes on `main`. Deploys to AWS in about 1 minute.

1. Builds the gateway Docker image → pushes to **AWS ECR** (tag: `:gateway`)
2. Builds the mock service image → pushes to **AWS ECR** (tag: `:mock`)
3. SSHs into EC2, pulls the new images, restarts the stack via `docker-compose`

Workflow: [`.github/workflows/cd.yml`](.github/workflows/cd.yml)

### Infrastructure

| Component | Details |
|---|---|
| Compute | AWS EC2 t3.micro : Ubuntu 24.04 (`eu-north-1`) |
| Container registry | AWS ECR : single repo, two tags (`:gateway`, `:mock`) |
| Orchestration | `docker-compose` : Redis + Gateway + Mock service |
| Secrets | GitHub Actions secrets : AWS keys, SSH key, ECR URI |
| Cost protection | AWS Budget alert at $0.01 + CloudWatch alarm auto-stops instance on sustained high CPU |

---

## Architecture Notes

### Request lifecycle

```
Request
  → LoggingMiddleware (log + attach request_id)
  → router match (longest prefix)
  → method check
  → JWT validation (if auth_required)
  → rate limit check (Redis INCR + EXPIRE via Lua)
  → cache lookup (Redis GET, GET requests only)
  → circuit breaker guard (before_call)
  → httpx proxy with retry loop
  → circuit breaker update (on_success / on_failure)
  → cache write (successful GET responses)
  → add X-* response headers
  → LoggingMiddleware (log response + latency)
Response
```

### Why Lua for rate limiting?

The `INCR` + `EXPIRE` must be atomic. Without Lua, a race between two requests could both see `count == 1` and both set the TTL, potentially resetting the window. The Lua script runs atomically on the Redis server.

### Circuit breaker state machine

```
     failures >= threshold
CLOSED ──────────────────────► OPEN
  ▲                               │
  │  on_success()      recovery   │
  │                   timeout     │
  └──── HALF_OPEN ◄───────────────┘
           │
           │ probe fails
           └──────────────────────► OPEN
```

State is stored in Redis so all gateway replicas share it, no split brain across replicas.

---

## Author

**Dimitrios Dalaklidis**  
[LinkedIn](https://www.linkedin.com/in/dimitris-dalaklidis-a72838397/) · [GitHub](https://github.com/DimitriosDalaklidhs)
