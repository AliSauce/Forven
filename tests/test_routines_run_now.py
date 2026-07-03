"""Routine "Run now" — manual dispatch of a routine's brain_invoke job.

Covers ``forven.control_plane.routines.dispatch_routine_now`` and the
``POST /api/routines/{id}/run`` route. The manual dispatch must enqueue a
``brain_invoke`` task using the SAME payload shape the scheduler builds for
cron fires (prompt + tools_context + channel), differing only by ``source``.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forven.control_plane import routines as r
from forven.db import get_db, init_db
from forven.routers import routines as routines_router


def _make(**overrides) -> int:
    base = dict(
        name="daily-roundup",
        prompt="summarize the day",
        cron_expr="0 17 * * *",
        tools_context="research",
        channel="ops",
    )
    base.update(overrides)
    return r.create_routine(**base)


def _latest_brain_invoke_task() -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, type, payload, status FROM tasks "
            "WHERE type = 'brain_invoke' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "no brain_invoke task was enqueued"
    out = dict(row)
    out["payload"] = json.loads(out["payload"] or "{}")
    return out


@pytest.fixture
def client(forven_db) -> TestClient:
    app = FastAPI()
    app.include_router(routines_router.router)
    return TestClient(app)


# --- control-plane dispatch ----------------------------------------------

def test_dispatch_enqueues_brain_invoke_task(forven_db) -> None:
    init_db()
    routine_id = _make()

    result = r.dispatch_routine_now(routine_id)

    assert result["task_id"] > 0
    assert result["routine_id"] == routine_id
    assert result["display_id"].startswith("T")

    task = _latest_brain_invoke_task()
    assert task["id"] == result["task_id"]
    assert task["type"] == "brain_invoke"

    payload = task["payload"]
    # Reuses the scheduler's cron-fire payload shape, manual source.
    assert payload["source"] == "manual_routine"
    assert payload["routine_id"] == routine_id
    assert payload["message"] == "summarize the day"
    assert payload["tools_context"] == "research"
    # The bot posts the response to this Discord channel (payload.channel
    # delivery, same as generic brain_invoke scheduler jobs).
    assert payload["channel"] == "ops"


def test_dispatch_records_run(forven_db) -> None:
    init_db()
    routine_id = _make()

    r.dispatch_routine_now(routine_id)

    routine = r.get_routine(routine_id)
    assert routine is not None
    assert routine["last_status"] == "dispatched"
    assert routine["last_run_at"]


def test_dispatch_missing_routine_raises(forven_db) -> None:
    init_db()
    with pytest.raises(r.RoutineValidationError):
        r.dispatch_routine_now(999999)


def test_dispatch_paused_routine_raises(forven_db) -> None:
    init_db()
    routine_id = _make(enabled=False)
    with pytest.raises(r.RoutineDispatchError):
        r.dispatch_routine_now(routine_id)


# --- HTTP route -----------------------------------------------------------

def test_run_route_dispatches_job(client: TestClient) -> None:
    init_db()
    routine_id = _make()

    resp = client.post(f"/api/routines/{routine_id}/run")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["routine_id"] == routine_id
    assert body["task_id"] > 0
    assert body["display_id"].startswith("T")

    task = _latest_brain_invoke_task()
    assert task["id"] == body["task_id"]
    assert task["payload"]["source"] == "manual_routine"
    assert task["payload"]["message"] == "summarize the day"


def test_run_route_missing_routine_returns_404(client: TestClient) -> None:
    init_db()
    resp = client.post("/api/routines/999999/run")
    assert resp.status_code == 404


def test_run_route_paused_routine_returns_409(client: TestClient) -> None:
    init_db()
    routine_id = _make(enabled=False)
    resp = client.post(f"/api/routines/{routine_id}/run")
    assert resp.status_code == 409


# --- channel picker --------------------------------------------------------

def test_channels_endpoint_prefers_live_bot_list(client: TestClient) -> None:
    """When the bot has published its guild channels, the picker serves them
    (raw ids + names) so the page works on any user's Discord server."""
    init_db()
    from forven.db import kv_set
    from forven.discord_channels import AVAILABLE_CHANNELS_KV_KEY

    kv_set(
        AVAILABLE_CHANNELS_KV_KEY,
        {
            "updated_at": "2026-07-02T00:00:00+00:00",
            "channels": [{"id": "123456", "name": "updates"}],
        },
    )
    resp = client.get("/api/routines/channels")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "discord"
    assert {"id": "123456", "label": "#updates"} in body["channels"]


def test_channels_endpoint_falls_back_to_alias_map(client: TestClient) -> None:
    """Without a bot-published list, the static alias map still populates the
    picker (and 'channels' is not captured by the /{routine_id} route)."""
    init_db()
    resp = client.get("/api/routines/channels")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "aliases"
    assert any(c["id"] == "ops" for c in body["channels"])
