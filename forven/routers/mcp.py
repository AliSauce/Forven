"""Phase 4 / P4-T07 — MCP servers + grants HTTP API.

Endpoints:
    GET    /api/mcp/servers              — list all + status
    POST   /api/mcp/servers              — create (re-registers tools if enabled)
    GET    /api/mcp/servers/{name}       — detail + tool count
    PUT    /api/mcp/servers/{name}       — update (re-registers on enable change)
    DELETE /api/mcp/servers/{name}       — unregister + cascade grants
    POST   /api/mcp/servers/{name}/test  — handshake probe
    GET    /api/mcp/servers/{name}/tools — live discovered list (no register)

    GET    /api/mcp/agents/{agent_id}/grants
    POST   /api/mcp/agents/{agent_id}/grants
    DELETE /api/mcp/agents/{agent_id}/grants/{server_name}

All endpoints require operator auth (same dependency the agents router
uses). Per-route try/except wraps DB and MCP calls so a malformed
request becomes 4xx rather than a 500 stack trace through to the
operator UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forven.agents import mcp_client
from forven.agents.mcp_client import (
    MCPProtocolError,
    close as mcp_close,
    connect as mcp_connect,
    list_tools as mcp_list_tools,
    register_server_tools,
    unregister_server_tools,
)
from forven.agents.tool_registry import _REGISTRY
from forven.api_security import require_operator_access
from forven.db import get_db

log = logging.getLogger("forven.routers.mcp")

router = APIRouter(tags=["mcp"], dependencies=[Depends(require_operator_access)])


VALID_TRANSPORTS = {"stdio", "http"}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class MCPServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    transport: str  # 'stdio' | 'http'
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    tools_include: list[str] | None = None
    tools_exclude: list[str] = Field(default_factory=list)


class MCPServerUpdate(BaseModel):
    transport: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    enabled: bool | None = None
    tools_include: list[str] | None = None
    tools_exclude: list[str] | None = None


class GrantBody(BaseModel):
    server_name: str = Field(..., min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: Any) -> dict:
    return {
        "name": row["name"],
        "transport": row["transport"],
        "command": row["command"],
        "args": json.loads(row["args_json"] or "[]"),
        "env": json.loads(row["env_json"] or "{}"),
        "url": row["url"],
        "headers": json.loads(row["headers_json"] or "{}"),
        "enabled": bool(row["enabled"]),
        "tools_include": (
            json.loads(row["tools_include_json"]) if row["tools_include_json"] else None
        ),
        "tools_exclude": json.loads(row["tools_exclude_json"] or "[]"),
        "last_status": row["last_status"],
        "last_status_at": row["last_status_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _registered_tool_count(server: str) -> int:
    prefix = f"mcp_{server}_"
    return sum(1 for k in _REGISTRY.keys() if k.startswith(prefix))


def _validate_create(body: MCPServerCreate) -> None:
    if body.transport not in VALID_TRANSPORTS:
        raise HTTPException(400, f"transport must be one of {sorted(VALID_TRANSPORTS)}")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(400, "stdio transport requires 'command'")
    if body.transport == "http" and not body.url:
        raise HTTPException(400, "http transport requires 'url'")


# ---------------------------------------------------------------------------
# Server CRUD
# ---------------------------------------------------------------------------

@router.get("/api/mcp/servers")
def list_servers() -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_servers ORDER BY name"
        ).fetchall()
    out = []
    for row in rows:
        d = _row_to_dict(row)
        d["registered_tool_count"] = _registered_tool_count(d["name"])
        out.append(d)
    return {"servers": out}


@router.post("/api/mcp/servers", status_code=201)
async def create_server(body: MCPServerCreate) -> dict:
    _validate_create(body)

    def _insert_sync():
        with get_db() as conn:
            existing = conn.execute(
                "SELECT name FROM mcp_servers WHERE name = ?", (body.name,)
            ).fetchone()
            if existing:
                raise HTTPException(409, f"server {body.name!r} already exists")
            conn.execute(
                "INSERT INTO mcp_servers (name, transport, command, args_json, "
                "env_json, url, headers_json, enabled, tools_include_json, "
                "tools_exclude_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body.name,
                    body.transport,
                    body.command,
                    json.dumps(body.args),
                    json.dumps(body.env),
                    body.url,
                    json.dumps(body.headers),
                    1 if body.enabled else 0,
                    json.dumps(body.tools_include) if body.tools_include is not None else None,
                    json.dumps(body.tools_exclude),
                ),
            )
            conn.commit()
            return conn.execute(
                "SELECT * FROM mcp_servers WHERE name = ?", (body.name,)
            ).fetchone()

    # Off-loop: a SQLite write can wait up to the 60s busy timeout under
    # contention — that must never stall the request loop (drops the live WS).
    row = await asyncio.to_thread(_insert_sync)

    if body.enabled:
        try:
            await register_server_tools(body.name)
        except Exception as exc:
            log.warning("create_server: tool registration for %r failed: %s", body.name, exc)

    return _row_to_dict(row)


@router.get("/api/mcp/servers/{name}")
def get_server(name: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_servers WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"server {name!r} not found")
    d = _row_to_dict(row)
    d["registered_tool_count"] = _registered_tool_count(name)
    return d


@router.put("/api/mcp/servers/{name}")
async def update_server(name: str, body: MCPServerUpdate) -> dict:
    def _update_sync():
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM mcp_servers WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, f"server {name!r} not found")
            was_enabled = bool(row["enabled"])

            sets: list[str] = []
            params: list[Any] = []
            if body.transport is not None:
                if body.transport not in VALID_TRANSPORTS:
                    raise HTTPException(400, f"transport must be one of {sorted(VALID_TRANSPORTS)}")
                sets.append("transport = ?"); params.append(body.transport)
            if body.command is not None:
                sets.append("command = ?"); params.append(body.command)
            if body.args is not None:
                sets.append("args_json = ?"); params.append(json.dumps(body.args))
            if body.env is not None:
                sets.append("env_json = ?"); params.append(json.dumps(body.env))
            if body.url is not None:
                sets.append("url = ?"); params.append(body.url)
            if body.headers is not None:
                sets.append("headers_json = ?"); params.append(json.dumps(body.headers))
            if body.enabled is not None:
                sets.append("enabled = ?"); params.append(1 if body.enabled else 0)
            if body.tools_include is not None:
                sets.append("tools_include_json = ?"); params.append(json.dumps(body.tools_include))
            if body.tools_exclude is not None:
                sets.append("tools_exclude_json = ?"); params.append(json.dumps(body.tools_exclude))

            if sets:
                sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')")
                params.append(name)
                conn.execute(
                    f"UPDATE mcp_servers SET {', '.join(sets)} WHERE name = ?",
                    params,
                )
                conn.commit()

            row = conn.execute(
                "SELECT * FROM mcp_servers WHERE name = ?", (name,)
            ).fetchone()
            return row, was_enabled, bool(row["enabled"])

    # Off-loop: SQLite writes can wait on the busy timeout (see create_server).
    row, was_enabled, is_enabled = await asyncio.to_thread(_update_sync)

    # Re-register on enable transitions or whenever an enabled server's
    # config changed (cheap to do — closes and re-lists).
    try:
        if was_enabled or is_enabled:
            unregister_server_tools(name)
        if is_enabled:
            await register_server_tools(name)
    except Exception as exc:
        log.warning("update_server: re-registration for %r failed: %s", name, exc)

    return _row_to_dict(row)


@router.delete("/api/mcp/servers/{name}", status_code=204)
def delete_server(name: str) -> None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT name FROM mcp_servers WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"server {name!r} not found")
        # FK ON DELETE CASCADE handles agent_mcp_grants.
        conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        conn.commit()
    unregister_server_tools(name)


# ---------------------------------------------------------------------------
# Server probes
# ---------------------------------------------------------------------------

@router.post("/api/mcp/servers/{name}/test")
async def test_server(name: str) -> dict:
    config = mcp_client.load_server_config(name)
    if config is None:
        raise HTTPException(404, f"server {name!r} not found")
    try:
        session = await mcp_connect(config)
    except MCPProtocolError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        return {
            "ok": True,
            "protocol_version": session.server_protocol_version,
            "server_info": session.server_info,
        }
    finally:
        await mcp_close(session)


@router.get("/api/mcp/servers/{name}/tools")
async def list_server_tools(name: str) -> dict:
    config = mcp_client.load_server_config(name)
    if config is None:
        raise HTTPException(404, f"server {name!r} not found")
    try:
        session = await mcp_connect(config)
    except Exception as exc:
        raise HTTPException(502, f"connect failed: {exc}") from exc
    try:
        tools = await mcp_list_tools(session)
        return {"tools": tools}
    finally:
        await mcp_close(session)


# ---------------------------------------------------------------------------
# Agent grants
# ---------------------------------------------------------------------------

@router.get("/api/mcp/agents/{agent_id}/grants")
def list_agent_grants(agent_id: str) -> dict:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT server_name, granted_at, granted_by FROM agent_mcp_grants "
            "WHERE agent_id = ? ORDER BY server_name",
            (agent_id,),
        ).fetchall()
    return {
        "agent_id": agent_id,
        "grants": [
            {
                "server_name": row["server_name"],
                "granted_at": row["granted_at"],
                "granted_by": row["granted_by"],
            }
            for row in rows
        ],
    }


@router.post("/api/mcp/agents/{agent_id}/grants", status_code=201)
def create_grant(agent_id: str, body: GrantBody) -> dict:
    with get_db() as conn:
        server_row = conn.execute(
            "SELECT name FROM mcp_servers WHERE name = ?", (body.server_name,)
        ).fetchone()
        if server_row is None:
            raise HTTPException(404, f"server {body.server_name!r} not found")
        agent_row = conn.execute(
            "SELECT id FROM agents WHERE id = ?", (agent_id,)
        ).fetchone()
        if agent_row is None:
            raise HTTPException(404, f"agent {agent_id!r} not found")
        conn.execute(
            "INSERT OR IGNORE INTO agent_mcp_grants (agent_id, server_name) "
            "VALUES (?, ?)",
            (agent_id, body.server_name),
        )
        conn.commit()
    return {"agent_id": agent_id, "server_name": body.server_name, "ok": True}


@router.delete("/api/mcp/agents/{agent_id}/grants/{server_name}", status_code=204)
def delete_grant(agent_id: str, server_name: str) -> None:
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM agent_mcp_grants WHERE agent_id = ? AND server_name = ?",
            (agent_id, server_name),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "grant not found")
