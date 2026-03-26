"""
Lightweight mock downstream service for local development/testing.
Simulates user-service, order-service, and failure scenarios.

Run on port 8010:
    uvicorn mock_service:app --port 8010
"""

import asyncio
import random
import time
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Downstream Service", version="1.0.0")

# ── Fake data ────────────────────────────────────────────────────────────────

_USERS: Dict[int, Dict] = {
    1: {"id": 1, "name": "Alice Johnson", "email": "alice@example.com", "role": "admin"},
    2: {"id": 2, "name": "Bob Smith",     "email": "bob@example.com",   "role": "user"},
    3: {"id": 3, "name": "Carol Davis",   "email": "carol@example.com", "role": "user"},
}

_ORDERS: Dict[int, Dict] = {
    101: {"id": 101, "user_id": 1, "item": "Laptop",     "total": 1299.99, "status": "shipped"},
    102: {"id": 102, "user_id": 2, "item": "Headphones", "total":  149.00, "status": "pending"},
    103: {"id": 103, "user_id": 1, "item": "Keyboard",   "total":   89.99, "status": "delivered"},
}

_failure_mode: Dict[str, Any] = {
    "enabled": False,
    "rate": 0.0,
    "delay": 0.0,
    "status": 500,
}


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _maybe_fail() -> None:
    if _failure_mode["enabled"]:
        if _failure_mode["delay"]:
            await asyncio.sleep(_failure_mode["delay"])
        if random.random() < _failure_mode["rate"]:
            raise HTTPException(
                status_code=_failure_mode["status"],
                detail="Simulated downstream failure",
            )


# ── Users ────────────────────────────────────────────────────────────────────

@app.get("/users")
async def list_users():
    await _maybe_fail()
    return {"users": list(_USERS.values()), "total": len(_USERS)}


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    await _maybe_fail()
    if user_id not in _USERS:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return _USERS[user_id]


@app.post("/users", status_code=201)
async def create_user(request: Request):
    await _maybe_fail()
    body = await request.json()
    new_id = max(_USERS.keys()) + 1
    user = {"id": new_id, **body}
    _USERS[new_id] = user
    return user


@app.put("/users/{user_id}")
async def update_user(user_id: int, request: Request):
    await _maybe_fail()
    if user_id not in _USERS:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    body = await request.json()
    _USERS[user_id].update(body)
    return _USERS[user_id]


@app.delete("/users/{user_id}")
async def delete_user(user_id: int):
    await _maybe_fail()
    if user_id not in _USERS:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    del _USERS[user_id]
    return {"deleted": user_id}


# ── Orders ────────────────────────────────────────────────────────────────────

@app.get("/orders")
async def list_orders(user_id: int | None = Query(None)):
    await _maybe_fail()
    orders = list(_ORDERS.values())
    if user_id:
        orders = [o for o in orders if o["user_id"] == user_id]
    return {"orders": orders, "total": len(orders)}


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    await _maybe_fail()
    if order_id not in _ORDERS:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return _ORDERS[order_id]


@app.post("/orders", status_code=201)
async def create_order(request: Request):
    await _maybe_fail()
    body = await request.json()
    new_id = max(_ORDERS.keys()) + 1
    order = {"id": new_id, "status": "pending", **body}
    _ORDERS[new_id] = order
    return order


# ── Mock: echo / health / failure control ────────────────────────────────────

@app.get("/mock/echo")
@app.post("/mock/echo")
@app.put("/mock/echo")
@app.delete("/mock/echo")
async def echo(request: Request):
    await _maybe_fail()
    body_bytes = await request.body()
    return {
        "method": request.method,
        "path": request.url.path,
        "headers": dict(request.headers),
        "body": body_bytes.decode("utf-8", errors="replace"),
        "timestamp": time.time(),
    }


@app.get("/mock/slow")
async def slow(delay: float = Query(2.0, ge=0, le=30)):
    """Simulate a slow upstream response."""
    await asyncio.sleep(delay)
    return {"message": f"Responded after {delay}s"}


@app.get("/mock/status/{code}")
async def fixed_status(code: int):
    """Return any HTTP status code."""
    return JSONResponse(
        content={"status": code, "description": "Forced status response"},
        status_code=code,
    )


@app.post("/mock/failure-mode")
async def set_failure_mode(
    enabled: bool = False,
    rate: float = Query(0.5, ge=0.0, le=1.0),
    delay: float = Query(0.0, ge=0.0, le=30.0),
    status: int = Query(500, ge=400, le=599),
):
    """Control simulated failure behaviour."""
    _failure_mode.update({"enabled": enabled, "rate": rate, "delay": delay, "status": status})
    return {"failure_mode": _failure_mode}


@app.get("/mock/failure-mode")
async def get_failure_mode():
    return {"failure_mode": _failure_mode}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "mock-downstream"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mock_service:app", host="0.0.0.0", port=8010, reload=True)