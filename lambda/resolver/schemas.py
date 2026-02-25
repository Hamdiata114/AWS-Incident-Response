"""Resolver agent Pydantic models and tool schemas."""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Proposal output models
# ---------------------------------------------------------------------------

class AWSAPICall(BaseModel):
    service: str          # "iam" | "lambda"
    operation: str        # "put_role_policy" | "delete_function_concurrency"
    parameters: dict      # exact boto3 kwargs
    risk_level: str       # "low" | "medium" | "high"
    requires_approval: bool
    reasoning: str


class RemediationProposal(BaseModel):
    incident_id: str
    fault_types: list[str]
    actions: list[AWSAPICall]
    reasoning: str


# ---------------------------------------------------------------------------
# Tool argument schemas
# ---------------------------------------------------------------------------

class GetBaselineIAMArgs(BaseModel):
    role_name: str


class GetCurrentConcurrencyArgs(BaseModel):
    lambda_name: str


TOOL_ARG_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_baseline_iam": GetBaselineIAMArgs,
    "get_current_concurrency": GetCurrentConcurrencyArgs,
}


# ---------------------------------------------------------------------------
# Tool response schemas
# ---------------------------------------------------------------------------

class BaselineIAMResponse(BaseModel):
    role_name: str
    policy_name: str
    expected_policy: dict
    current_policy: dict | None
    drift: bool


class ConcurrencyResponse(BaseModel):
    lambda_name: str
    reserved_concurrency: int | None
    is_throttled: bool


TOOL_RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_baseline_iam": BaselineIAMResponse,
    "get_current_concurrency": ConcurrencyResponse,
}
