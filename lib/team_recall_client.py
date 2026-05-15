"""
team_recall_client.py — minimal Supabase RPC client for team-recall.

Stdlib-only (urllib + json). No supabase-js / supabase-py dependency, so
the plugin stays zero-install. Calls the SECURITY DEFINER RPCs defined
in `supabase/migrations/<ts>_team_recall.sql`.

Used by:
  - hooks/on-session-stop.py — calls ingest_team_recall_session(...)
  - tools/team-recall-search.py — calls search_team_recall(...)

All errors are caught and surfaced via raised TeamRecallError. The
hook layer wraps every call in try/except so a Postgres outage never
breaks a session.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TeamRecallError(RuntimeError):
    """Raised when an RPC call to Supabase fails."""


@dataclass
class _RpcResponse:
    status: int
    body: Any


def _rpc(
    url: str, service_key: str, fn_name: str, args: dict[str, Any], timeout: float = 8.0
) -> _RpcResponse:
    """Call a Postgres RPC via PostgREST."""
    endpoint = f"{url.rstrip('/')}/rest/v1/rpc/{fn_name}"
    data = json.dumps(args).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "params=single-object",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                body = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                body = raw
            return _RpcResponse(status=resp.status, body=body)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise TeamRecallError(f"HTTP {e.code} from {fn_name}: {raw[:300]}") from None
    except urllib.error.URLError as e:
        raise TeamRecallError(f"Network error calling {fn_name}: {e.reason}") from None
    except Exception as e:
        raise TeamRecallError(f"Unexpected error calling {fn_name}: {e}") from None


def ingest_session(
    *,
    url: str,
    service_key: str,
    session_id: str,
    engineer_id: str,
    project_slug: str,
    started_at: str,
    ended_at: str,
    turn_count: int,
    tool_call_freq: dict[str, int],
    git_branch: str | None,
    cwd: str | None,
    messages: list[dict[str, Any]],
) -> dict:
    """Push a parsed session into team_recall via the SECURITY DEFINER RPC.

    `messages` is a list of dicts with shape:
        {uuid, parent_uuid, timestamp (ISO), role, content}

    Idempotent on session_id and message uuid. Returns the RPC's JSONB
    payload: {session_id, messages_inserted, messages_total}.
    """
    args = {
        "p_session_id": session_id,
        "p_engineer_id": engineer_id,
        "p_project_slug": project_slug,
        "p_started_at": started_at,
        "p_ended_at": ended_at,
        "p_turn_count": turn_count,
        "p_tool_call_freq": tool_call_freq,
        "p_git_branch": git_branch,
        "p_cwd": cwd,
        "p_messages": messages,
    }
    resp = _rpc(url, service_key, "ingest_team_recall_session", args)
    if not isinstance(resp.body, dict):
        raise TeamRecallError(f"Unexpected ingest response shape: {resp.body!r}")
    return resp.body


def search(
    *,
    url: str,
    service_key: str,
    query: str,
    limit: int = 10,
    engineer_id: str | None = None,
    project_slug: str | None = None,
) -> list[dict]:
    """Run a websearch_to_tsquery FTS over team-recall messages."""
    args: dict[str, Any] = {
        "p_query": query,
        "p_limit": limit,
        "p_engineer_id": engineer_id,
        "p_project_slug": project_slug,
    }
    resp = _rpc(url, service_key, "search_team_recall", args)
    if resp.body is None:
        return []
    if not isinstance(resp.body, list):
        raise TeamRecallError(f"Unexpected search response shape: {resp.body!r}")
    return resp.body


def list_recent(
    *,
    url: str,
    service_key: str,
    limit: int = 10,
    engineer_id: str | None = None,
    project_slug: str | None = None,
    min_turns: int = 2,
) -> list[dict]:
    args: dict[str, Any] = {
        "p_limit": limit,
        "p_engineer_id": engineer_id,
        "p_project_slug": project_slug,
        "p_min_turns": min_turns,
    }
    resp = _rpc(url, service_key, "list_recent_team_recall", args)
    if resp.body is None:
        return []
    if not isinstance(resp.body, list):
        raise TeamRecallError(f"Unexpected list_recent response shape: {resp.body!r}")
    return resp.body
