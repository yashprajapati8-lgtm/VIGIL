"""Load generator: steady checkout traffic, so the signals are never 0/0.

Also exports what users feel (checkout success/failure and latency), which is the
signal VIGIL verifies recovery against. The RPS control endpoint sits on the
chaos admin port, so only the chaos controller can start a "flash sale".
"""

import asyncio
import contextlib
import os
import random
import time

import httpx
from fastapi import Header
from pydantic import BaseModel, Field
from prometheus_client import Counter, Gauge, Histogram

from .common import LATENCY_BUCKETS, create_app, log, require_token

PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")
SKUS = ["SKU-HEADPHONES", "SKU-KEYBOARD", "SKU-MONITOR", "SKU-MOUSE", "SKU-WEBCAM"]
MAX_IN_FLIGHT = 300

CHECKOUTS = Counter("checkout_requests_total", "Checkouts attempted by users", ["outcome"])
CHECKOUT_DURATION = Histogram(
    "checkout_duration_seconds", "End-to-end checkout latency", buckets=LATENCY_BUCKETS
)
TARGET_RPS = Gauge("loadgen_target_rps", "Configured checkout rate")


class State:
    rps: float = float(os.getenv("RPS", "20"))
    in_flight: int = 0


async def _one_checkout(client: httpx.AsyncClient) -> None:
    State.in_flight += 1
    start = time.perf_counter()
    try:
        resp = await client.post("/pay", json={"sku": random.choice(SKUS), "qty": random.randint(1, 3)})
        outcome = "ok" if resp.status_code < 400 else ("client_error" if resp.status_code < 500 else "failed")
    except httpx.HTTPError:
        outcome = "failed"
    finally:
        State.in_flight -= 1
    CHECKOUTS.labels(outcome).inc()
    CHECKOUT_DURATION.observe(time.perf_counter() - start)


async def _drive() -> None:
    # No keep-alive: every request resolves the `payment` alias again, so a
    # rollback or an extra replica receives traffic immediately.
    limits = httpx.Limits(max_keepalive_connections=0, max_connections=MAX_IN_FLIGHT)
    async with httpx.AsyncClient(base_url=PAYMENT_URL, timeout=5.0, limits=limits) as client:
        pending: set[asyncio.Task] = set()
        while True:
            TARGET_RPS.set(State.rps)
            if State.rps <= 0:
                await asyncio.sleep(0.5)
                continue
            if State.in_flight < MAX_IN_FLIGHT:
                task = asyncio.create_task(_one_checkout(client))
                pending.add(task)
                task.add_done_callback(pending.discard)
            else:
                CHECKOUTS.labels("dropped").inc()
            # Jittered spacing so traffic looks organic rather than metronomic.
            await asyncio.sleep(random.expovariate(State.rps))


_driver: asyncio.Task | None = None


async def _start() -> None:
    global _driver
    _driver = asyncio.create_task(_drive())
    log.info("load generator targeting %s at %.0f rps", PAYMENT_URL, State.rps)


async def _stop() -> None:
    if _driver:
        _driver.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _driver


class RateUpdate(BaseModel):
    rps: float = Field(ge=0, le=500)


def _rps_routes(admin) -> None:
    """Rate control lives on the chaos admin app: separate port, token required."""

    @admin.get("/rps")
    def get_rps(x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        return {"rps": State.rps}

    @admin.post("/rps")
    def set_rps(update: RateUpdate, x_chaos_token: str | None = Header(None)):
        require_token(x_chaos_token)
        State.rps = update.rps
        log.warning("load set to %.0f rps", State.rps)
        return {"rps": State.rps}


app = create_app("loadgen", on_startup=_start, on_shutdown=_stop, extend_admin=_rps_routes)
