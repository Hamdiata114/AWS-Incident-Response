"""Tests for agent.py — 30 tests, one behavior each."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import botocore.exceptions
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from agent import (
    McpInitError,
    RECURSION_LIMIT,
    agent_reason,
    build_graph,
    check_deadline,
    classify_error,
    create_tools,
    execute_tools,
    get_mcp_api_key,
    run_agent,
    validate_tool_args,
    validate_tool_response,
)
from schemas import (
    AgentError,
    Diagnosis,
    LogsResponse,
    MockToolProvider,
    TokenUsage,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "test"}}, "InvokeModel"
    )


def _make_state(deadline_remaining=300, messages=None, token_usage=None):
    import time
    return {
        "messages": messages or [SystemMessage(content="test")],
        "incident": {"lambda_name": "data-processor"},
        "incident_id": "data-processor#2025-01-15T10:30:00Z",
        "diagnosis": None,
        "deadline": time.time() + deadline_remaining,
        "token_usage": token_usage or [],
    }


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------

class TestClassifyError:
    def test_classify_timeout(self):
        assert classify_error(TimeoutError("t")).category == "mcp_connection"

    def test_classify_connection_error(self):
        assert classify_error(ConnectionError("c")).category == "mcp_connection"

    def test_classify_os_error(self):
        assert classify_error(OSError("o")).category == "mcp_connection"

    def test_classify_access_denied(self):
        assert classify_error(_client_error("AccessDeniedException")).category == "bedrock_auth"

    def test_classify_unauthorized(self):
        assert classify_error(_client_error("UnauthorizedException")).category == "bedrock_auth"

    def test_classify_throttling(self):
        assert classify_error(_client_error("ThrottlingException")).category == "bedrock_transient"

    def test_classify_service_unavailable(self):
        assert classify_error(_client_error("ServiceUnavailableException")).category == "bedrock_transient"

    def test_classify_model_timeout(self):
        assert classify_error(_client_error("ModelTimeoutException")).category == "bedrock_transient"

    def test_classify_unknown_client_error(self):
        assert classify_error(_client_error("SomeOtherError")).category == "unknown"

    def test_classify_mcp_init(self):
        assert classify_error(McpInitError("init fail")).category == "mcp_init"


# ---------------------------------------------------------------------------
# check_deadline
# ---------------------------------------------------------------------------

class TestCheckDeadline:
    def test_check_deadline_under_90s(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - 89) is True

    def test_check_deadline_over_90s(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - 91) is False

    def test_check_deadline_exactly_90s(self):
        state = _make_state()
        assert check_deadline(state, now=state["deadline"] - 90) is False


# ---------------------------------------------------------------------------
# validate_tool_args
# ---------------------------------------------------------------------------

class TestValidateToolArgs:
    def test_validate_args_valid(self):
        result = validate_tool_args("get_recent_logs", {"lambda_name": "test"})
        assert result == {"lambda_name": "test"}

    def test_validate_args_missing_field(self):
        with pytest.raises(ValidationError):
            validate_tool_args("get_recent_logs", {})

    def test_validate_args_extra_fields(self):
        result = validate_tool_args("get_recent_logs", {"lambda_name": "test", "extra": "ignored"})
        assert result == {"lambda_name": "test"}

    def test_validate_args_unknown_tool(self):
        with pytest.raises(KeyError):
            validate_tool_args("nonexistent_tool", {"lambda_name": "test"})


# ---------------------------------------------------------------------------
# validate_tool_response
# ---------------------------------------------------------------------------

class TestValidateToolResponse:
    def test_validate_response_valid_logs(self):
        raw = json.dumps({"log_group": "/aws/test", "events": []})
        result = validate_tool_response("get_recent_logs", raw)
        assert isinstance(result, LogsResponse)

    def test_validate_response_invalid_json(self):
        result = validate_tool_response("get_recent_logs", "not json")
        assert isinstance(result, str)
        assert "Invalid JSON" in result

    def test_validate_response_missing_field(self):
        raw = json.dumps({"events": []})  # missing log_group
        result = validate_tool_response("get_recent_logs", raw)
        assert isinstance(result, str)
        assert "validation failed" in result

    def test_validate_response_with_error_field(self):
        raw = json.dumps({"log_group": "/aws/test", "events": [], "error": "timeout"})
        result = validate_tool_response("get_recent_logs", raw)
        assert isinstance(result, LogsResponse)
        assert result.error == "timeout"


# ---------------------------------------------------------------------------
# agent_reason
# ---------------------------------------------------------------------------

class TestAgentReason:
    def test_agent_reason_calls_bedrock(self):
        mock_llm = AsyncMock()
        response = AIMessage(content="Let me investigate.")
        response.response_metadata = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        mock_llm.ainvoke.return_value = response

        state = _make_state(deadline_remaining=300)
        result = asyncio.get_event_loop().run_until_complete(agent_reason(state, mock_llm))

        mock_llm.ainvoke.assert_called_once()
        assert len(result["messages"]) == 1

    def test_agent_reason_forces_diagnosis_near_deadline(self):
        mock_llm = AsyncMock()
        response = AIMessage(content="Submitting now.")
        response.response_metadata = {"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}
        mock_llm.ainvoke.return_value = response

        state = _make_state(deadline_remaining=60)
        asyncio.get_event_loop().run_until_complete(agent_reason(state, mock_llm))

        call_args = mock_llm.ainvoke.call_args[0][0]
        injected = [m for m in call_args if isinstance(m, HumanMessage) and "Time is running out" in m.content]
        assert len(injected) == 1

    def test_agent_reason_extracts_token_usage(self):
        mock_llm = AsyncMock()
        response = AIMessage(content="ok")
        response.response_metadata = {"usage": {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240}}
        mock_llm.ainvoke.return_value = response

        state = _make_state(deadline_remaining=300)
        result = asyncio.get_event_loop().run_until_complete(agent_reason(state, mock_llm))

        assert len(result["token_usage"]) == 1
        assert result["token_usage"][0].total_tokens == 240


# ---------------------------------------------------------------------------
# execute_tools
# ---------------------------------------------------------------------------

class TestExecuteTools:
    def test_execute_tools_valid(self):
        provider = MockToolProvider({
            "get_recent_logs": json.dumps({"log_group": "/aws/test", "events": []})
        })
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_recent_logs", "args": {"lambda_name": "test"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert len(result["messages"]) == 1
        assert "log_group" in result["messages"][0].content

    def test_execute_tools_invalid_args_no_mcp(self):
        provider = MockToolProvider({})
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_recent_logs", "args": {}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert "error" in result["messages"][0].content

    def test_execute_tools_invalid_response(self):
        provider = MockToolProvider({
            "get_recent_logs": "not json"
        })
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_recent_logs", "args": {"lambda_name": "test"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert "error" in result["messages"][0].content


# ---------------------------------------------------------------------------
# create_tools
# ---------------------------------------------------------------------------

class TestCreateTools:
    def test_create_tools_returns_four(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        assert len(tools) == 4

    def test_create_tools_has_submit_diagnosis(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        names = [t.name for t in tools]
        assert "submit_diagnosis" in names

    def test_create_tools_has_mcp_tools(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        names = set(t.name for t in tools)
        assert {"get_recent_logs", "get_iam_state", "get_lambda_config"}.issubset(names)


# ---------------------------------------------------------------------------
# build_graph
# ---------------------------------------------------------------------------

class TestBuildGraph:
    @patch("agent.ChatBedrockConverse")
    def test_build_graph_compiles(self, mock_bedrock):
        mock_bedrock.return_value.bind_tools.return_value = MagicMock()
        provider = MockToolProvider({})
        tools = create_tools(provider)
        graph = build_graph(tools, provider)
        assert graph is not None

    def test_build_graph_recursion_limit(self):
        assert RECURSION_LIMIT == 12


# ---------------------------------------------------------------------------
# get_mcp_api_key
# ---------------------------------------------------------------------------

class TestGetMcpApiKey:
    @patch("agent.boto3.client")
    def test_get_mcp_api_key_returns_value(self, mock_client):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "super-secret-key"}
        }
        mock_client.return_value = mock_ssm

        result = get_mcp_api_key()
        assert result == "super-secret-key"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/incident-response/mcp-api-key", WithDecryption=True
        )

    @patch("agent.boto3.client")
    def test_get_mcp_api_key_missing_param(self, mock_client):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = _client_error("ParameterNotFound")
        mock_client.return_value = mock_ssm

        with pytest.raises(botocore.exceptions.ClientError):
            get_mcp_api_key()


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------

class TestRunAgent:
    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_run_agent_success(self, mock_exec, mock_key):
        diag = Diagnosis(
            root_cause="test", fault_types=["permission_loss"],
            affected_resources=["fn"], severity="high",
            evidence=[], remediation_plan=[],
        )
        mock_exec.return_value = diag
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"lambda_name": "test"}, "id1", MagicMock())
        )
        assert result == diag

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_run_agent_retries_mcp_connection(self, mock_sleep, mock_exec, mock_key):
        mock_exec.side_effect = [ConnectionError("fail"), Diagnosis(
            root_cause="ok", fault_types=[], affected_resources=[], severity="low",
            evidence=[], remediation_plan=[],
        )]
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"lambda_name": "test"}, "id1", MagicMock())
        )
        assert result.root_cause == "ok"
        assert mock_exec.call_count == 2

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_run_agent_retries_bedrock_transient(self, mock_sleep, mock_exec, mock_key):
        mock_exec.side_effect = [
            _client_error("ThrottlingException"),
            Diagnosis(
                root_cause="ok", fault_types=[], affected_resources=[], severity="low",
                evidence=[], remediation_plan=[],
            ),
        ]
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"lambda_name": "test"}, "id1", MagicMock())
        )
        assert result.root_cause == "ok"

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_run_agent_no_retry_bedrock_auth(self, mock_exec, mock_key):
        mock_exec.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(AgentError) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                run_agent({"lambda_name": "test"}, "id1", MagicMock())
            )
        assert exc_info.value.category == "bedrock_auth"
        assert mock_exec.call_count == 1

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_run_agent_raises_after_max_retries(self, mock_sleep, mock_exec, mock_key):
        mock_exec.side_effect = ConnectionError("fail")
        with pytest.raises(AgentError) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                run_agent({"lambda_name": "test"}, "id1", MagicMock())
            )
        assert exc_info.value.category == "mcp_connection"
        assert mock_exec.call_count == 2

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_run_agent_backoff_timing(self, mock_sleep, mock_exec, mock_key):
        mock_exec.side_effect = ConnectionError("fail")
        with pytest.raises(AgentError):
            asyncio.get_event_loop().run_until_complete(
                run_agent({"lambda_name": "test"}, "id1", MagicMock())
            )
        mock_sleep.assert_called_once_with(1)  # 2^0 = 1

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_run_agent_returns_none_no_diagnosis(self, mock_exec, mock_key):
        mock_exec.return_value = None
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"lambda_name": "test"}, "id1", MagicMock())
        )
        assert result is None
