"""Tests for schemas.py — 34 tests, one behavior each."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from schemas import (
    AgentError,
    Diagnosis,
    EvidencePointer,
    GetIAMStateArgs,
    GetLambdaConfigArgs,
    GetLogsArgs,
    IAMStateResponse,
    LambdaConfigResponse,
    LogEvent,
    LogsResponse,
    McpToolProvider,
    MockToolProvider,
    RemediationStep,
    TOOL_ARG_SCHEMAS,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# LogEvent
# ---------------------------------------------------------------------------

class TestLogEvent:
    def test_log_event_valid(self):
        e = LogEvent(timestamp="2025-01-01T00:00:00Z", message="hello")
        assert e.timestamp == "2025-01-01T00:00:00Z"
        assert e.message == "hello"

    def test_log_event_missing_timestamp(self):
        with pytest.raises(ValidationError):
            LogEvent(message="hello")

    def test_log_event_missing_message(self):
        with pytest.raises(ValidationError):
            LogEvent(timestamp="2025-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# LogsResponse
# ---------------------------------------------------------------------------

class TestLogsResponse:
    def test_logs_response_valid(self):
        r = LogsResponse(
            log_group="/aws/lambda/test",
            events=[{"timestamp": "t", "message": "m"}],
        )
        assert r.log_group == "/aws/lambda/test"
        assert len(r.events) == 1

    def test_logs_response_empty_events(self):
        r = LogsResponse(log_group="/aws/lambda/test", events=[])
        assert r.events == []

    def test_logs_response_with_error(self):
        r = LogsResponse(log_group="/aws/lambda/test", events=[], error="timeout")
        assert r.error == "timeout"

    def test_logs_response_missing_log_group(self):
        with pytest.raises(ValidationError):
            LogsResponse(events=[])


# ---------------------------------------------------------------------------
# IAMStateResponse
# ---------------------------------------------------------------------------

class TestIAMStateResponse:
    def test_iam_state_valid(self):
        r = IAMStateResponse(
            role_name="my-role",
            inline_policies={"policy1": {"Statement": []}},
            attached_policies=["arn:aws:iam::policy/ReadOnly"],
        )
        assert r.role_name == "my-role"

    def test_iam_state_empty_policies(self):
        r = IAMStateResponse(
            role_name="my-role",
            inline_policies={},
            attached_policies=[],
        )
        assert r.inline_policies == {}
        assert r.attached_policies == []

    def test_iam_state_missing_role_name(self):
        with pytest.raises(ValidationError):
            IAMStateResponse(inline_policies={}, attached_policies=[])


# ---------------------------------------------------------------------------
# LambdaConfigResponse
# ---------------------------------------------------------------------------

class TestLambdaConfigResponse:
    def test_lambda_config_full(self):
        r = LambdaConfigResponse(
            FunctionName="data-processor",
            Runtime="python3.12",
            Handler="handler.handler",
            Role="arn:aws:iam::role/my-role",
            MemorySize=128,
            Timeout=30,
            State="Active",
            ReservedConcurrentExecutions=10,
        )
        assert r.FunctionName == "data-processor"
        assert r.ReservedConcurrentExecutions == 10

    def test_lambda_config_minimal(self):
        r = LambdaConfigResponse(FunctionName="data-processor")
        assert r.Runtime is None
        assert r.ReservedConcurrentExecutions is None

    def test_lambda_config_missing_function_name(self):
        with pytest.raises(ValidationError):
            LambdaConfigResponse()

    def test_lambda_config_zero_concurrency(self):
        r = LambdaConfigResponse(
            FunctionName="data-processor",
            ReservedConcurrentExecutions=0,
        )
        assert r.ReservedConcurrentExecutions == 0


# ---------------------------------------------------------------------------
# EvidencePointer
# ---------------------------------------------------------------------------

class TestEvidencePointer:
    def test_evidence_pointer_valid(self):
        e = EvidencePointer(
            tool="get_iam_state",
            field="inline_policies.Statement",
            value="[]",
            interpretation="No policies attached",
        )
        assert e.tool == "get_iam_state"

    def test_evidence_pointer_missing_tool(self):
        with pytest.raises(ValidationError):
            EvidencePointer(field="f", value="v", interpretation="i")


# ---------------------------------------------------------------------------
# RemediationStep
# ---------------------------------------------------------------------------

class TestRemediationStep:
    def test_remediation_step_valid(self):
        s = RemediationStep(
            action="Restore S3 IAM policy",
            details="Re-attach policy",
            evidence_basis=[0, 1],
            risk_level="medium",
            requires_approval=False,
        )
        assert s.action == "Restore S3 IAM policy"

    def test_remediation_step_empty_evidence_basis(self):
        s = RemediationStep(
            action="a",
            details="d",
            evidence_basis=[],
            risk_level="low",
            requires_approval=False,
        )
        assert s.evidence_basis == []

    def test_remediation_step_missing_risk_level(self):
        with pytest.raises(ValidationError):
            RemediationStep(
                action="a",
                details="d",
                evidence_basis=[],
                requires_approval=False,
            )


# ---------------------------------------------------------------------------
# Diagnosis
# ---------------------------------------------------------------------------

class TestDiagnosis:
    def test_diagnosis_valid(self):
        d = Diagnosis(
            root_cause="S3 policy revoked",
            fault_types=["permission_loss"],
            affected_resources=["data-processor"],
            severity="high",
            evidence=[
                EvidencePointer(tool="get_iam_state", field="f", value="v", interpretation="i")
            ],
            remediation_plan=[
                RemediationStep(
                    action="restore",
                    details="d",
                    evidence_basis=[0],
                    risk_level="low",
                    requires_approval=False,
                )
            ],
        )
        assert d.root_cause == "S3 policy revoked"

    def test_diagnosis_empty_fault_types(self):
        d = Diagnosis(
            root_cause="unknown",
            fault_types=[],
            affected_resources=[],
            severity="low",
            evidence=[],
            remediation_plan=[],
        )
        assert d.fault_types == []

    def test_diagnosis_missing_root_cause(self):
        with pytest.raises(ValidationError):
            Diagnosis(
                fault_types=[],
                affected_resources=[],
                severity="low",
                evidence=[],
                remediation_plan=[],
            )


# ---------------------------------------------------------------------------
# TokenUsage
# ---------------------------------------------------------------------------

class TestTokenUsage:
    def test_token_usage_valid(self):
        t = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert t.total_tokens == 150

    def test_token_usage_missing_field(self):
        with pytest.raises(ValidationError):
            TokenUsage(prompt_tokens=100, completion_tokens=50)


# ---------------------------------------------------------------------------
# Tool arg schemas
# ---------------------------------------------------------------------------

class TestGetLogsArgs:
    def test_get_logs_args_valid(self):
        a = GetLogsArgs(lambda_name="data-processor")
        assert a.lambda_name == "data-processor"

    def test_get_logs_args_missing(self):
        with pytest.raises(ValidationError):
            GetLogsArgs()


class TestGetIAMArgs:
    def test_get_iam_args_valid(self):
        a = GetIAMStateArgs(lambda_name="data-processor")
        assert a.lambda_name == "data-processor"

    def test_get_iam_args_missing(self):
        with pytest.raises(ValidationError):
            GetIAMStateArgs()


class TestGetConfigArgs:
    def test_get_config_args_valid(self):
        a = GetLambdaConfigArgs(lambda_name="data-processor")
        assert a.lambda_name == "data-processor"

    def test_get_config_args_missing(self):
        with pytest.raises(ValidationError):
            GetLambdaConfigArgs()


# ---------------------------------------------------------------------------
# TOOL_ARG_SCHEMAS
# ---------------------------------------------------------------------------

class TestToolArgSchemas:
    def test_tool_arg_schemas_three_entries(self):
        assert len(TOOL_ARG_SCHEMAS) == 3

    def test_tool_arg_schemas_correct_keys(self):
        assert set(TOOL_ARG_SCHEMAS.keys()) == {
            "get_recent_logs",
            "get_iam_state",
            "get_lambda_config",
        }


# ---------------------------------------------------------------------------
# McpToolProvider
# ---------------------------------------------------------------------------

class TestMcpToolProvider:
    def test_mcp_provider_returns_text(self):
        mock_session = AsyncMock()
        content_item = SimpleNamespace(text='{"log_group": "/aws/test", "events": []}')
        mock_session.call_tool.return_value = SimpleNamespace(content=[content_item])

        provider = McpToolProvider(mock_session)
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("get_recent_logs", {"lambda_name": "test"})
        )
        assert result == '{"log_group": "/aws/test", "events": []}'

    def test_mcp_provider_empty_returns_error(self):
        mock_session = AsyncMock()
        mock_session.call_tool.return_value = SimpleNamespace(content=[])

        provider = McpToolProvider(mock_session)
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("get_recent_logs", {"lambda_name": "test"})
        )
        assert result == '{"error": "Tool returned empty response"}'


# ---------------------------------------------------------------------------
# MockToolProvider
# ---------------------------------------------------------------------------

class TestMockToolProvider:
    def test_mock_provider_known_tool(self):
        provider = MockToolProvider({"get_recent_logs": '{"log_group": "x", "events": []}'})
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("get_recent_logs", {})
        )
        assert '"log_group"' in result

    def test_mock_provider_unknown_tool(self):
        provider = MockToolProvider({})
        result = asyncio.get_event_loop().run_until_complete(
            provider.call_tool("no_such_tool", {})
        )
        assert result == '{"error": "unknown tool"}'


# ---------------------------------------------------------------------------
# AgentError
# ---------------------------------------------------------------------------

class TestAgentError:
    def test_agent_error_stores_fields(self):
        e = AgentError("mcp_connection", "timeout after 10s")
        assert e.category == "mcp_connection"
        assert e.message == "timeout after 10s"

    def test_agent_error_str_format(self):
        e = AgentError("bedrock_auth", "access denied")
        assert str(e) == "[bedrock_auth] access denied"

    def test_agent_error_is_exception(self):
        e = AgentError("unknown", "something broke")
        assert isinstance(e, Exception)
