"""LangGraph agent for AWS incident remediation proposal."""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import os
import time
from typing import Annotated, TypedDict

import boto3
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import ValidationError

from schemas import (
    GetBaselineIAMArgs,
    GetCurrentConcurrencyArgs,
    RemediationProposal,
    TOOL_ARG_SCHEMAS,
    TOOL_RESPONSE_SCHEMAS,
)
from shared.schemas import AgentError, McpToolProvider, TokenUsage, ToolProvider
from shared.agent_utils import (
    PERMANENT_CATEGORIES,
    check_deadline,
    classify_error,
    serialize_messages,
    validate_tool_args as _validate_tool_args,
    validate_tool_response as _validate_tool_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BEDROCK_MODEL = "us.amazon.nova-2-lite-v1:0"
BEDROCK_REGION = "ca-central-1"
MCP_CONNECT_TIMEOUT = 10
MCP_INIT_TIMEOUT = 10
MAX_TOKENS_PER_INCIDENT = 50_000
RECURSION_LIMIT = 8

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")

SYSTEM_PROMPT = (
    "You are an AWS incident remediation specialist. Given a diagnosis of Lambda "
    "function failures, you produce concrete, executable remediation proposals with "
    "exact AWS API parameters.\n\n"
    "RULES:\n"
    "1. ONLY use data returned by your tools and the diagnosis. Never fabricate information.\n"
    "2. For each fault type, call the appropriate tool to gather current state.\n"
    "3. Produce exact boto3 kwargs for each remediation action.\n"
    "4. When ready, call submit_proposal with your complete proposal.\n\n"
    "FAULT TYPE → TOOL MAPPING:\n"
    "- permission_loss → get_baseline_iam(role_name='lab-lambda-baisc-role')\n"
    "  Remediation: put_role_policy with the expected_policy from baseline\n"
    "- throttling → get_current_concurrency(lambda_name='data-processor')\n"
    "  Remediation: delete_function_concurrency to remove the throttle\n\n"
    "REMEDIATION PATTERNS:\n"
    "- permission_loss: service='iam', operation='put_role_policy', parameters must include "
    "RoleName, PolicyName, PolicyDocument (the expected_policy from baseline)\n"
    "- throttling: service='lambda', operation='delete_function_concurrency', parameters must "
    "include FunctionName\n\n"
    "RISK LEVELS:\n"
    "- put_role_policy (restoring known-good): risk_level='medium', requires_approval=True\n"
    "- delete_function_concurrency: risk_level='low', requires_approval=False\n\n"
    "After gathering state from tools, call submit_proposal immediately."
)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class McpInitError(Exception):
    """Raised when MCP session.initialize() fails."""


# ---------------------------------------------------------------------------
# SSM secret fetch
# ---------------------------------------------------------------------------

def get_mcp_api_key() -> str:
    ssm = boto3.client("ssm", region_name=BEDROCK_REGION)
    resp = ssm.get_parameter(
        Name="/incident-response/mcp-api-key", WithDecryption=True
    )
    return resp["Parameter"]["Value"]


# ---------------------------------------------------------------------------
# Local wrappers — bind module-level schema dicts
# ---------------------------------------------------------------------------

def validate_tool_args(tool_name: str, arguments: dict) -> dict:
    return _validate_tool_args(tool_name, arguments, TOOL_ARG_SCHEMAS)


def validate_tool_response(tool_name: str, raw_json: str):
    return _validate_tool_response(tool_name, raw_json, TOOL_RESPONSE_SCHEMAS)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class ResolverState(TypedDict):
    messages: Annotated[list, add_messages]
    incident_id: str
    diagnosis: dict
    proposal: RemediationProposal | None
    deadline: float
    token_usage: Annotated[list[TokenUsage], operator.add]
    _nudged: bool


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

async def agent_reason(state: ResolverState, llm) -> dict:
    """Call LLM with current messages. Checks deadline and token budget."""
    messages = list(state["messages"])

    if check_deadline(state):
        messages.append(
            HumanMessage(
                content="Time is running out. Submit your proposal immediately "
                "with whatever information you have."
            )
        )

    total_tokens = sum(t.total_tokens for t in state.get("token_usage", []))
    if total_tokens >= MAX_TOKENS_PER_INCIDENT:
        messages.append(
            HumanMessage(content="Token budget exceeded. Submit your proposal immediately.")
        )

    logger.info("agent_reason: sending %d messages to LLM", len(messages))
    response = await llm.ainvoke(messages)

    has_tools = hasattr(response, "tool_calls") and bool(response.tool_calls)
    tool_names = [tc["name"] for tc in response.tool_calls] if has_tools else []
    logger.info(
        "agent_reason: response has_tool_calls=%s tool_names=%s content_preview=%.200s",
        has_tools, tool_names, response.content,
    )

    usage_data = response.response_metadata.get("usage", {})
    token_usage = TokenUsage(
        prompt_tokens=usage_data.get("prompt_tokens", 0),
        completion_tokens=usage_data.get("completion_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
    )

    return {
        "messages": [response],
        "token_usage": [token_usage],
    }


async def execute_tools(state: ResolverState, provider: ToolProvider) -> dict:
    """Validate args, call MCP tools, validate responses."""
    last_msg = state["messages"][-1]
    tool_messages = []

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        arguments = tc["args"]
        tool_call_id = tc["id"]

        if tool_name == "submit_proposal":
            continue

        try:
            validated_args = validate_tool_args(tool_name, arguments)
        except (ValidationError, KeyError) as e:
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"error": f"Invalid arguments: {e}"}),
                    tool_call_id=tool_call_id,
                )
            )
            continue

        raw_response = await provider.call_tool(tool_name, validated_args)
        logger.info("execute_tools: %s returned %d bytes", tool_name, len(raw_response))

        result = validate_tool_response(tool_name, raw_response)
        if isinstance(result, str):
            tool_messages.append(
                ToolMessage(
                    content=json.dumps({"error": result}),
                    tool_call_id=tool_call_id,
                )
            )
        else:
            tool_messages.append(
                ToolMessage(content=raw_response, tool_call_id=tool_call_id)
            )

    return {"messages": tool_messages}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_reason(state: ResolverState) -> str:
    """Route after agent_reason: tools, submit, or end."""
    last_msg = state["messages"][-1]
    if not hasattr(last_msg, "tool_calls") or not last_msg.tool_calls:
        if not state.get("proposal") and not state.get("_nudged"):
            logger.info("route_after_reason: NUDGE (no tool calls, no proposal)")
            return "nudge"
        logger.info("route_after_reason: END (no tool calls)")
        return "end"
    for tc in last_msg.tool_calls:
        if tc["name"] == "submit_proposal":
            logger.info("route_after_reason: SUBMIT")
            return "submit"
    logger.info("route_after_reason: TOOLS → %s", [tc["name"] for tc in last_msg.tool_calls])
    return "tools"


def nudge_proposal(state: ResolverState) -> dict:
    """Inject a reminder to call submit_proposal."""
    logger.info("nudge_proposal: reminding LLM to call submit_proposal")
    return {
        "messages": [
            HumanMessage(
                content="You have gathered enough information. You MUST now call the "
                "submit_proposal tool with your remediation proposal. Do not respond "
                "with text — call submit_proposal immediately."
            )
        ],
        "_nudged": True,
    }


def extract_proposal(state: ResolverState) -> dict:
    """Extract proposal from submit_proposal tool call."""
    last_msg = state["messages"][-1]
    for tc in last_msg.tool_calls:
        if tc["name"] == "submit_proposal":
            return {"proposal": RemediationProposal(**tc["args"])}
    return {}


# ---------------------------------------------------------------------------
# Tool + graph creation
# ---------------------------------------------------------------------------

def _noop(**kwargs):
    raise RuntimeError("Tools are executed via execute_tools node")


def create_tools(provider: ToolProvider) -> list[StructuredTool]:
    """Create tool definitions for the LLM."""
    return [
        StructuredTool(
            name="get_baseline_iam",
            description="Compare current IAM inline policy against known-good baseline. Returns drift info.",
            func=_noop,
            args_schema=GetBaselineIAMArgs,
        ),
        StructuredTool(
            name="get_current_concurrency",
            description="Get Lambda reserved concurrency and flag if throttled.",
            func=_noop,
            args_schema=GetCurrentConcurrencyArgs,
        ),
        StructuredTool(
            name="submit_proposal",
            description="Submit your final remediation proposal with exact AWS API calls.",
            func=_noop,
            args_schema=RemediationProposal,
        ),
    ]


def build_graph(tools: list[StructuredTool], provider: ToolProvider):
    """Build and compile the LangGraph resolver agent."""
    llm = ChatBedrockConverse(model=BEDROCK_MODEL, region_name=BEDROCK_REGION)
    llm_with_tools = llm.bind_tools(tools)

    async def _agent_reason(state: ResolverState) -> dict:
        return await agent_reason(state, llm_with_tools)

    async def _execute_tools(state: ResolverState) -> dict:
        return await execute_tools(state, provider)

    graph = StateGraph(ResolverState)
    graph.add_node("agent_reason", _agent_reason)
    graph.add_node("execute_tools", _execute_tools)
    graph.add_node("extract_proposal", extract_proposal)
    graph.add_node("nudge_proposal", nudge_proposal)

    graph.set_entry_point("agent_reason")
    graph.add_conditional_edges(
        "agent_reason",
        route_after_reason,
        {"tools": "execute_tools", "submit": "extract_proposal", "nudge": "nudge_proposal", "end": END},
    )
    graph.add_edge("execute_tools", "agent_reason")
    graph.add_edge("nudge_proposal", "agent_reason")
    graph.add_edge("extract_proposal", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

async def _execute_agent(diagnosis, incident_id, lambda_context, api_key):
    """Single attempt: connect MCP, build graph, invoke, return proposal."""
    headers = {"Authorization": f"Bearer {api_key}"}
    async with sse_client(MCP_SERVER_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            try:
                async with asyncio.timeout(MCP_INIT_TIMEOUT):
                    await session.initialize()
            except Exception as e:
                raise McpInitError(str(e)) from e

            provider = McpToolProvider(session)
            tools = create_tools(provider)
            graph = build_graph(tools, provider)

            remaining_ms = lambda_context.get_remaining_time_in_millis()
            deadline = time.time() + remaining_ms / 1000

            initial_state = {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(
                        content=(
                            f"Produce a remediation proposal for incident {incident_id}.\n"
                            f"Diagnosis:\n{json.dumps(diagnosis, indent=2)}"
                        )
                    ),
                ],
                "incident_id": incident_id,
                "diagnosis": diagnosis,
                "proposal": None,
                "deadline": deadline,
                "token_usage": [],
                "_nudged": False,
            }

            result = await graph.ainvoke(
                initial_state, config={"recursion_limit": RECURSION_LIMIT}
            )
            return {
                "proposal": result.get("proposal"),
                "reasoning_chain": serialize_messages(result.get("messages", [])),
                "token_usage": [t.model_dump() for t in result.get("token_usage", [])],
            }


async def run_agent(diagnosis: dict, incident_id: str, lambda_context) -> dict:
    """Run the resolver agent with retry logic.

    Returns {"proposal": RemediationProposal | None, "reasoning_chain": [...], "token_usage": [...]}
    """
    max_retries = 2
    last_error = None
    api_key = get_mcp_api_key()

    for attempt in range(max_retries):
        try:
            return await _execute_agent(diagnosis, incident_id, lambda_context, api_key)
        except AgentError:
            raise
        except Exception as e:
            logger.exception("Attempt %d/%d exception", attempt + 1, max_retries)
            agent_error = classify_error(e)
            if agent_error.category in PERMANENT_CATEGORIES:
                raise agent_error
            last_error = agent_error
            logger.warning(
                "Attempt %d/%d failed: %s", attempt + 1, max_retries, agent_error
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    raise last_error
