"""Tests for resolver agent.py."""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import botocore.exceptions
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent import (
    McpInitError,
    RECURSION_LIMIT,
    agent_reason,
    build_graph,
    check_deadline,
    classify_error,
    create_tools,
    execute_tools,
    extract_proposal,
    get_mcp_api_key,
    nudge_proposal,
    route_after_reason,
    run_agent,
    validate_tool_args,
    validate_tool_response,
)
from schemas import (
    AWSAPICall,
    RemediationProposal,
)
from shared.schemas import AgentError, MockToolProvider, TokenUsage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "test"}}, "InvokeModel"
    )


def _make_state(deadline_remaining=300, messages=None, token_usage=None):
    return {
        "messages": messages or [SystemMessage(content="test")],
        "incident_id": "data-processor#2025-01-15T10:30:00Z",
        "diagnosis": {"fault_types": ["permission_loss"]},
        "proposal": None,
        "deadline": time.time() + deadline_remaining,
        "token_usage": token_usage or [],
        "_nudged": False,
    }


SAMPLE_IAM_RESPONSE = json.dumps({
    "role_name": "lab-lambda-baisc-role",
    "policy_name": "data-processor-access",
    "expected_policy": {"Version": "2012-10-17", "Statement": []},
    "current_policy": None,
    "drift": True,
})

SAMPLE_CONCURRENCY_RESPONSE = json.dumps({
    "lambda_name": "data-processor",
    "reserved_concurrency": 0,
    "is_throttled": True,
})

SAMPLE_PROPOSAL_ARGS = {
    "incident_id": "data-processor#2025-01-15T10:30:00Z",
    "fault_types": ["permission_loss"],
    "actions": [{
        "service": "iam",
        "operation": "put_role_policy",
        "parameters": {"RoleName": "lab-lambda-baisc-role", "PolicyName": "data-processor-access", "PolicyDocument": "{}"},
        "risk_level": "medium",
        "requires_approval": True,
        "reasoning": "Restore IAM policy to baseline",
    }],
    "reasoning": "IAM drift detected, restoring policy",
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_recursion_limit(self):
        assert RECURSION_LIMIT == 8


# ---------------------------------------------------------------------------
# validate_tool_args (resolver-specific wrappers)
# ---------------------------------------------------------------------------

class TestValidateToolArgs:
    def test_valid_iam(self):
        result = validate_tool_args("get_baseline_iam", {"role_name": "test-role"})
        assert result == {"role_name": "test-role"}

    def test_valid_concurrency(self):
        result = validate_tool_args("get_current_concurrency", {"lambda_name": "test"})
        assert result == {"lambda_name": "test"}

    def test_unknown_tool(self):
        with pytest.raises(KeyError):
            validate_tool_args("nonexistent", {})


# ---------------------------------------------------------------------------
# validate_tool_response (resolver-specific wrappers)
# ---------------------------------------------------------------------------

class TestValidateToolResponse:
    def test_valid_iam_response(self):
        result = validate_tool_response("get_baseline_iam", SAMPLE_IAM_RESPONSE)
        assert result.drift is True

    def test_valid_concurrency_response(self):
        result = validate_tool_response("get_current_concurrency", SAMPLE_CONCURRENCY_RESPONSE)
        assert result.is_throttled is True

    def test_invalid_json(self):
        result = validate_tool_response("get_baseline_iam", "not json")
        assert "Invalid JSON" in result


# ---------------------------------------------------------------------------
# agent_reason
# ---------------------------------------------------------------------------

class TestAgentReason:
    def test_calls_llm(self):
        mock_llm = AsyncMock()
        response = AIMessage(content="Let me check the baseline.")
        response.response_metadata = {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}
        mock_llm.ainvoke.return_value = response

        state = _make_state()
        result = asyncio.get_event_loop().run_until_complete(agent_reason(state, mock_llm))

        mock_llm.ainvoke.assert_called_once()
        assert len(result["messages"]) == 1
        assert result["token_usage"][0].total_tokens == 120

    def test_deadline_pressure(self):
        mock_llm = AsyncMock()
        response = AIMessage(content="Submitting now.")
        response.response_metadata = {"usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60}}
        mock_llm.ainvoke.return_value = response

        state = _make_state(deadline_remaining=60)
        asyncio.get_event_loop().run_until_complete(agent_reason(state, mock_llm))

        call_args = mock_llm.ainvoke.call_args[0][0]
        injected = [m for m in call_args if isinstance(m, HumanMessage) and "Time is running out" in m.content]
        assert len(injected) == 1


# ---------------------------------------------------------------------------
# execute_tools
# ---------------------------------------------------------------------------

class TestExecuteTools:
    def test_permission_loss_tool(self):
        provider = MockToolProvider({"get_baseline_iam": SAMPLE_IAM_RESPONSE})
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_baseline_iam", "args": {"role_name": "lab-lambda-baisc-role"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert len(result["messages"]) == 1
        assert "drift" in result["messages"][0].content

    def test_throttling_tool(self):
        provider = MockToolProvider({"get_current_concurrency": SAMPLE_CONCURRENCY_RESPONSE})
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_current_concurrency", "args": {"lambda_name": "data-processor"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert len(result["messages"]) == 1
        assert "is_throttled" in result["messages"][0].content

    def test_invalid_args(self):
        provider = MockToolProvider({})
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_baseline_iam", "args": {}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert "error" in result["messages"][0].content

    def test_skips_submit_proposal(self):
        provider = MockToolProvider({})
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "submit_proposal", "args": SAMPLE_PROPOSAL_ARGS, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])

        result = asyncio.get_event_loop().run_until_complete(execute_tools(state, provider))
        assert result["messages"] == []


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

class TestRouting:
    def test_route_to_tools(self):
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_baseline_iam", "args": {"role_name": "r"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        assert route_after_reason(state) == "tools"

    def test_route_to_submit(self):
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "submit_proposal", "args": SAMPLE_PROPOSAL_ARGS, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        assert route_after_reason(state) == "submit"

    def test_route_to_nudge(self):
        ai_msg = AIMessage(content="I'm thinking...")
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        state["_nudged"] = False
        state["proposal"] = None
        assert route_after_reason(state) == "nudge"

    def test_route_to_end_after_nudge(self):
        ai_msg = AIMessage(content="I'm done.")
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        state["_nudged"] = True
        assert route_after_reason(state) == "end"


# ---------------------------------------------------------------------------
# extract_proposal
# ---------------------------------------------------------------------------

class TestExtractProposal:
    def test_extracts(self):
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "submit_proposal", "args": SAMPLE_PROPOSAL_ARGS, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        result = extract_proposal(state)
        assert isinstance(result["proposal"], RemediationProposal)
        assert result["proposal"].fault_types == ["permission_loss"]

    def test_no_submit(self):
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "get_baseline_iam", "args": {"role_name": "r"}, "id": "tc1"}
        ])
        state = _make_state(messages=[SystemMessage(content="sys"), ai_msg])
        result = extract_proposal(state)
        assert result == {}


# ---------------------------------------------------------------------------
# nudge_proposal
# ---------------------------------------------------------------------------

class TestNudgeProposal:
    def test_nudge(self):
        state = _make_state()
        result = nudge_proposal(state)
        assert result["_nudged"] is True
        assert "submit_proposal" in result["messages"][0].content


# ---------------------------------------------------------------------------
# create_tools
# ---------------------------------------------------------------------------

class TestCreateTools:
    def test_returns_three(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        assert len(tools) == 3

    def test_has_submit_proposal(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        names = [t.name for t in tools]
        assert "submit_proposal" in names

    def test_has_mcp_tools(self):
        provider = MockToolProvider({})
        tools = create_tools(provider)
        names = set(t.name for t in tools)
        assert {"get_baseline_iam", "get_current_concurrency"}.issubset(names)


# ---------------------------------------------------------------------------
# build_graph
# ---------------------------------------------------------------------------

class TestBuildGraph:
    @patch("agent.ChatBedrockConverse")
    def test_compiles(self, mock_bedrock):
        mock_bedrock.return_value.bind_tools.return_value = MagicMock()
        provider = MockToolProvider({})
        tools = create_tools(provider)
        graph = build_graph(tools, provider)
        assert graph is not None


# ---------------------------------------------------------------------------
# run_agent
# ---------------------------------------------------------------------------

class TestRunAgent:
    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_success(self, mock_exec, mock_key):
        proposal = RemediationProposal(**SAMPLE_PROPOSAL_ARGS)
        mock_exec.return_value = {
            "proposal": proposal,
            "reasoning_chain": [],
            "token_usage": [{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}],
        }
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"fault_types": ["permission_loss"]}, "id1", MagicMock())
        )
        assert result["proposal"] == proposal

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_none_proposal(self, mock_exec, mock_key):
        mock_exec.return_value = {
            "proposal": None, "reasoning_chain": [], "token_usage": [],
        }
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"fault_types": ["permission_loss"]}, "id1", MagicMock())
        )
        assert result["proposal"] is None

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_retries_mcp_connection(self, mock_sleep, mock_exec, mock_key):
        proposal = RemediationProposal(**SAMPLE_PROPOSAL_ARGS)
        mock_exec.side_effect = [ConnectionError("fail"), {
            "proposal": proposal, "reasoning_chain": [], "token_usage": [],
        }]
        result = asyncio.get_event_loop().run_until_complete(
            run_agent({"fault_types": ["permission_loss"]}, "id1", MagicMock())
        )
        assert result["proposal"] is not None
        assert mock_exec.call_count == 2

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    def test_no_retry_bedrock_auth(self, mock_exec, mock_key):
        mock_exec.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(AgentError) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                run_agent({"fault_types": ["permission_loss"]}, "id1", MagicMock())
            )
        assert exc_info.value.category == "bedrock_auth"
        assert mock_exec.call_count == 1

    @patch("agent.get_mcp_api_key", return_value="key")
    @patch("agent._execute_agent")
    @patch("agent.asyncio.sleep", new_callable=AsyncMock)
    def test_raises_after_max_retries(self, mock_sleep, mock_exec, mock_key):
        mock_exec.side_effect = ConnectionError("fail")
        with pytest.raises(AgentError) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                run_agent({"fault_types": ["permission_loss"]}, "id1", MagicMock())
            )
        assert exc_info.value.category == "mcp_connection"
        assert mock_exec.call_count == 2
