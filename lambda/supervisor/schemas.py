"""Pydantic models, tool schemas, ToolProvider protocol, and AgentError."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Tool response models
# ---------------------------------------------------------------------------

class LogEvent(BaseModel):
    timestamp: str
    message: str


class LogsResponse(BaseModel):
    log_group: str
    events: list[LogEvent]
    error: str | None = None


class IAMStateResponse(BaseModel):
    role_name: str
    inline_policies: dict
    attached_policies: list[str]
    error: str | None = None


class LambdaConfigResponse(BaseModel):
    FunctionName: str
    Runtime: str | None = None
    Handler: str | None = None
    Role: str | None = None
    MemorySize: int | None = None
    Timeout: int | None = None
    State: str | None = None
    ReservedConcurrentExecutions: int | None = None


# ---------------------------------------------------------------------------
# Diagnosis output models
# ---------------------------------------------------------------------------

class EvidencePointer(BaseModel):
    tool: str
    field: str
    value: str
    interpretation: str


class RemediationStep(BaseModel):
    action: str
    details: str
    evidence_basis: list[int]
    risk_level: str
    requires_approval: bool


class Diagnosis(BaseModel):
    root_cause: str
    fault_types: list[str]
    affected_resources: list[str]
    severity: str
    evidence: list[EvidencePointer]
    remediation_plan: list[RemediationStep]


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


# ---------------------------------------------------------------------------
# Tool argument schemas
# ---------------------------------------------------------------------------

class GetLogsArgs(BaseModel):
    lambda_name: str


class GetIAMStateArgs(BaseModel):
    lambda_name: str


class GetLambdaConfigArgs(BaseModel):
    lambda_name: str


TOOL_ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_recent_logs": GetLogsArgs,
    "get_iam_state": GetIAMStateArgs,
    "get_lambda_config": GetLambdaConfigArgs,
}

TOOL_RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_recent_logs": LogsResponse,
    "get_iam_state": IAMStateResponse,
    "get_lambda_config": LambdaConfigResponse,
}


# ---------------------------------------------------------------------------
# ToolProvider protocol + implementations
# ---------------------------------------------------------------------------

@runtime_checkable
class ToolProvider(Protocol):
    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool by name and return the raw JSON string."""
        ...


class McpToolProvider:
    """Production implementation — delegates to an MCP ClientSession."""

    def __init__(self, session):
        self._session = session

    async def call_tool(self, name: str, arguments: dict) -> str:
        mcp_name = f"tool_{name}"
        result = await self._session.call_tool(mcp_name, arguments)
        if not result.content:
            return '{"error": "Tool returned empty response"}'
        return result.content[0].text


class MockToolProvider:
    """Test implementation — returns canned responses."""

    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    async def call_tool(self, name: str, arguments: dict) -> str:
        return self._responses.get(name, '{"error": "unknown tool"}')


# ---------------------------------------------------------------------------
# AgentError
# ---------------------------------------------------------------------------

class AgentError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(f"[{category}] {message}")
