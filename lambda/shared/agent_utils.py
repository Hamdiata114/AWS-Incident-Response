"""Shared agent utilities: error classification, deadline check, validation, serialization."""

from __future__ import annotations

import asyncio
import json
import time

import botocore.exceptions
from pydantic import ValidationError

from shared.schemas import AgentError


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEADLINE_BUFFER = 90
PERMANENT_CATEGORIES = frozenset({"bedrock_auth", "unknown"})


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def classify_error(exc: Exception) -> AgentError:
    """Map an exception to an AgentError with a category."""
    if isinstance(exc, BaseExceptionGroup):
        sub = exc.exceptions[0] if exc.exceptions else exc
        return classify_error(sub)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return AgentError("mcp_connection", f"Timeout: {exc}")
    if isinstance(exc, (ConnectionError, OSError)):
        return AgentError("mcp_connection", str(exc))
    # McpInitError is agent-specific; check by class name to avoid circular import
    if type(exc).__name__ == "McpInitError":
        return AgentError("mcp_init", str(exc))
    if isinstance(exc, botocore.exceptions.ClientError):
        code = exc.response["Error"]["Code"]
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return AgentError("bedrock_auth", str(exc))
        if code in (
            "ThrottlingException",
            "ServiceUnavailableException",
            "ModelTimeoutException",
        ):
            return AgentError("bedrock_transient", str(exc))
        return AgentError("unknown", str(exc))
    return AgentError("unknown", str(exc))


# ---------------------------------------------------------------------------
# Deadline check
# ---------------------------------------------------------------------------

def check_deadline(state: dict, now: float | None = None) -> bool:
    """Return True if remaining time is under DEADLINE_BUFFER seconds."""
    if now is None:
        now = time.time()
    remaining = state["deadline"] - now
    return remaining < DEADLINE_BUFFER


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_tool_args(tool_name: str, arguments: dict, schemas: dict) -> dict:
    """Validate tool arguments via a schemas dict. Raises ValidationError or KeyError."""
    schema = schemas[tool_name]
    validated = schema(**arguments)
    return validated.model_dump()


def validate_tool_response(tool_name: str, raw_json: str, schemas: dict):
    """Validate tool response. Returns Pydantic model on success, error string on failure."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON response: {e}"

    schema = schemas.get(tool_name)
    if schema is None:
        return data

    try:
        return schema(**data)
    except ValidationError as e:
        return f"Response validation failed: {e}"


# ---------------------------------------------------------------------------
# Message serialization
# ---------------------------------------------------------------------------

def serialize_messages(messages) -> list[dict]:
    """Convert LangChain messages into a readable step-by-step reasoning chain."""
    steps = []
    step_num = 0

    for m in messages:
        msg_type = type(m).__name__

        if msg_type == "SystemMessage":
            continue

        if msg_type == "HumanMessage":
            content = m.content if isinstance(m.content, str) else str(m.content)
            if "submit_diagnosis" in content.lower():
                step_num += 1
                steps.append({
                    "step": step_num,
                    "action": "nudge",
                    "detail": content,
                })
            elif step_num == 0:
                step_num += 1
                steps.append({
                    "step": step_num,
                    "action": "incident_received",
                    "detail": content[:500],
                })
            else:
                step_num += 1
                steps.append({
                    "step": step_num,
                    "action": "system_message",
                    "detail": content[:300],
                })
            continue

        if msg_type == "AIMessage":
            if hasattr(m, "tool_calls") and m.tool_calls:
                for tc in m.tool_calls:
                    step_num += 1
                    if tc["name"] == "submit_diagnosis":
                        steps.append({
                            "step": step_num,
                            "action": "submit_diagnosis",
                            "diagnosis": tc["args"],
                        })
                    else:
                        steps.append({
                            "step": step_num,
                            "action": "tool_call",
                            "tool": tc["name"],
                            "args": tc["args"],
                        })
            elif m.content:
                content = m.content if isinstance(m.content, str) else str(m.content)
                if content.strip():
                    step_num += 1
                    steps.append({
                        "step": step_num,
                        "action": "reasoning",
                        "detail": content[:500],
                    })
            continue

        if msg_type == "ToolMessage":
            step_num += 1
            content = m.content if isinstance(m.content, str) else str(m.content)
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "error" in data:
                    steps.append({
                        "step": step_num,
                        "action": "tool_error",
                        "error": data["error"],
                    })
                elif isinstance(data, dict):
                    summary = {k: v for k, v in data.items() if k != "events"}
                    if "events" in data:
                        summary["event_count"] = len(data["events"])
                    steps.append({
                        "step": step_num,
                        "action": "tool_result",
                        "summary": summary,
                    })
                else:
                    steps.append({
                        "step": step_num,
                        "action": "tool_result",
                        "summary": content[:300],
                    })
            except (json.JSONDecodeError, TypeError):
                steps.append({
                    "step": step_num,
                    "action": "tool_result",
                    "summary": content[:300],
                })
            continue

    return steps
