"""Muse Spark tool-calling loop over Chat Completions (tool_choice "auto").

Request shape follows the live docs at https://dev.meta.ai/docs (tool-calling):
OpenAI SDK with base_url https://api.meta.ai/v1, `tools` + `tool_choice`,
one `tool` message per call carrying its `tool_call_id`, max 6 tool rounds
enforced in our own loop. Model-generated arguments are validated
client-side before execution.
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

from app import config
from app.db import now_iso
from app.prompts import SYSTEM_PROMPT
from app.tools import TOOL_DEFS, TOOL_FUNCS

MAX_TOOL_ROUNDS = 6
HISTORY_TURNS = 20
TOKEN_LOG = Path("token_usage.log")

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI

        key = os.environ.get("MODEL_API_KEY", "")
        if not key:
            raise RuntimeError("MODEL_API_KEY is not set. Copy .env.example to .env and fill it in.")
        _client = OpenAI(base_url=config.META_BASE_URL, api_key=key)
    return _client


def load_history(conn: sqlite3.Connection, shop_id: str, limit: int = HISTORY_TURNS) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM conversation_turns WHERE shop_id = ? ORDER BY id DESC LIMIT ?",
        (shop_id, limit),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_turn(conn: sqlite3.Connection, shop_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO conversation_turns (shop_id, role, content) VALUES (?, ?, ?)",
        (shop_id, role, content),
    )
    conn.execute(
        """DELETE FROM conversation_turns WHERE shop_id = ? AND id NOT IN (
             SELECT id FROM conversation_turns WHERE shop_id = ? ORDER BY id DESC LIMIT ?)""",
        (shop_id, shop_id, HISTORY_TURNS),
    )
    conn.commit()


def _log_usage(shop_id: str, model: str, usage, round_no: int) -> None:
    try:
        entry = {
            "ts": now_iso(),
            "shop": shop_id,
            "model": model,
            "round": round_no,
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "total_tokens": getattr(usage, "total_tokens", 0) or 0,
        }
        with open(TOKEN_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _complete(messages: list) -> object:
    """Thin seam over the API call so tests can stub it."""
    return _get_client().chat.completions.create(
        model=config.model_name(), messages=messages, tools=TOOL_DEFS, tool_choice="auto"
    )


def _run_tool(conn: sqlite3.Connection, shop_id: str, name: str, raw_args: str | None) -> str:
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        return "Bad tool arguments (not JSON). Retry with a valid JSON object."
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"Unknown tool: {name}. Use one of the provided tools."
    if not isinstance(args, dict):
        return "Bad tool arguments (must be an object). Retry."
    try:
        params = inspect.signature(func).parameters
        accepted = {k: v for k, v in args.items() if k in params}
        return func(conn, shop_id, **accepted)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}."
    except Exception as e:  # noqa: BLE001 - keep the conversation alive, log the bug
        traceback.print_exc()
        return f"{name} failed: {e}."


def _print_api_error(e: Exception) -> None:
    print("Sisi API error (full body):", file=sys.stderr)
    body = getattr(e, "body", None)
    if body is not None:
        try:
            print(json.dumps(body, indent=2)[:4000], file=sys.stderr)
        except (TypeError, ValueError):
            print(str(body)[:4000], file=sys.stderr)
    else:
        print(repr(e)[:4000], file=sys.stderr)
    status = getattr(e, "status_code", None)
    if status is not None:
        print(f"status_code: {status}", file=sys.stderr)


def chat(conn: sqlite3.Connection, shop_id: str, text: str) -> str:
    model = config.model_name()
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += load_history(conn, shop_id)
    messages.append({"role": "user", "content": text})
    save_turn(conn, shop_id, "user", text)
    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        try:
            resp = _complete(messages)
        except Exception as e:  # noqa: BLE001 - API outage must not crash the chat
            _print_api_error(e)
            reply = "My signal dropped just now. Please say that again."
            save_turn(conn, shop_id, "assistant", reply)
            return reply
        _log_usage(shop_id, model, getattr(resp, "usage", None), round_no)
        msg = resp.choices[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])
        if not calls:
            reply = (msg.content or "").strip() or "Say that again for me?"
            save_turn(conn, shop_id, "assistant", reply)
            return reply
        messages.append(msg)
        for call in calls:
            result = _run_tool(conn, shop_id, call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    reply = "That one is tricky. Ask me again in fewer words?"
    save_turn(conn, shop_id, "assistant", reply)
    return reply
