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


def get_client():
    """Shared OpenAI-compatible client for the Meta Model API."""
    return _get_client()


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


def log_usage(shop_id: str, model: str, usage, round_no: int = 1) -> None:
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
    return get_client().chat.completions.create(
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


def print_api_error(e: Exception) -> None:
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


def _shop_context(conn: sqlite3.Connection, shop_id: str) -> str:
    """Compact live snapshot (stock, debtors, pending reminders) for the intent engine."""
    lines: list[str] = []
    try:
        rows = conn.execute(
            "SELECT name, stock_qty, price_cents FROM products"
            " WHERE shop_id = ? ORDER BY name LIMIT 15", (shop_id,)).fetchall()
        if rows:
            lines.append("STOCK (name | on hand | price):")
            for r in rows:
                price = f"R{r['price_cents'] / 100:.2f}" if r["price_cents"] else "no price"
                lines.append(f"- {r['name']} | {r['stock_qty']:g} | {price}")
        debts = conn.execute(
            "SELECT c.name AS name,"
            " COALESCE((SELECT SUM(amount_cents) FROM credit_entries e"
            "  WHERE e.customer_id = c.id AND e.reversed = 0), 0) -"
            " COALESCE((SELECT SUM(amount_cents) FROM payments p"
            "  WHERE p.customer_id = c.id AND p.reversed = 0), 0) AS bal"
            " FROM customers c WHERE c.shop_id = ? ORDER BY bal DESC LIMIT 10",
            (shop_id,)).fetchall()
        debts = [d for d in debts if d["bal"] > 0]
        if debts:
            lines.append("DEBTORS (name owes):")
            for d in debts:
                lines.append(f"- {d['name']} owes R{d['bal'] / 100:.2f}")
        rems = conn.execute(
            "SELECT due_at, summary FROM reminders"
            " WHERE shop_id = ? AND status = 'pending'"
            " ORDER BY due_at LIMIT 5", (shop_id,)).fetchall()
        if rems:
            lines.append("PENDING REMINDERS (due | what):")
            for m in rems:
                lines.append(f"- {m['due_at']} | {m['summary']}")
    except Exception:  # noqa: BLE001 - context is a hint, never fatal
        return ""
    return "\n".join(lines)


def chat(conn: sqlite3.Connection, shop_id: str, text: str) -> str:
    reply, _ = chat_full(conn, shop_id, text)
    return reply


def chat_full(conn: sqlite3.Connection, shop_id: str,
              text: str) -> tuple[str, list[str] | None]:
    """chat() plus reply-button options when the model asked to clarify."""
    model = config.model_name()
    system = SYSTEM_PROMPT
    ctx = _shop_context(conn, shop_id)
    if ctx:
        system += ("\n\nLIVE SHOP CONTEXT (facts for this shop; use the tools to"
                   " act on them, never guess these):\n" + ctx)
    messages: list = [{"role": "system", "content": system}]
    messages += load_history(conn, shop_id)
    messages.append({"role": "user", "content": text})
    save_turn(conn, shop_id, "user", text)
    options: list[str] | None = None
    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        try:
            resp = _complete(messages)
        except Exception as e:  # noqa: BLE001 - API outage must not crash the chat
            print_api_error(e)
            from app.i18n import t as _t
            reply = _t("llm_dropped")
            save_turn(conn, shop_id, "assistant", reply)
            return reply, None
        log_usage(shop_id, model, getattr(resp, "usage", None), round_no)
        msg = resp.choices[0].message
        calls = list(getattr(msg, "tool_calls", None) or [])
        if not calls:
            from app.i18n import t as _t
            reply = (msg.content or "").strip() or _t("llm_say_again")
            save_turn(conn, shop_id, "assistant", reply)
            return reply, options
        messages.append(msg)
        for call in calls:
            if call.function.name == "ask_clarify":
                options = _clarify_options(call.function.arguments)
            result = _run_tool(conn, shop_id, call.function.name, call.function.arguments)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
    from app.i18n import t as _t
    reply = _t("llm_tricky")
    save_turn(conn, shop_id, "assistant", reply)
    return reply, options


def _clarify_options(raw_args: str | None) -> list[str] | None:
    """Button labels (max 3, WhatsApp-sized) from an ask_clarify call."""
    try:
        opts = json.loads(raw_args or "{}").get("options") or []
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(opts, str):
        opts = [opts]
    labels = [str(o).strip()[:20] for o in opts if str(o).strip()][:3]
    return labels or None
