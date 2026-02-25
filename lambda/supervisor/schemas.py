"""Supervisor-specific Pydantic models and tool schemas.

Shared classes (AgentError, TokenUsage, ToolProvider, McpToolProvider,
MockToolProvider) are re-exported from shared.schemas for backwards compat.
"""

from __future__ import annotations

from pydantic import BaseModel

# Re-exports from shared
from shared.schemas import (  # noqa: F401
    AgentError,
    McpToolProvider,
    MockToolProvider,
    TokenUsage,
    ToolProvider,
)


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
