"""LangGraph agent for AWS incident diagnosis."""

from __future__ import annotations

import asyncio
import json
import logging
import operator
import os
import time
from typing import Annotated, TypedDict

import boto3
import botocore.exceptions
from langchain_aws import ChatBedrockConverse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from mcp import ClientSession
from mcp.client.sse import sse_client
from pydantic import ValidationError

from schemas import (
    AgentError,
    Diagnosis,
    GetIAMStateArgs,
    GetLambdaConfigArgs,
    GetLogsArgs,
    McpToolProvider,
    TokenUsage,
    TOOL_ARG_SCHEMAS,
    TOOL_RESPONSE_SCHEMAS,
    ToolProvider,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BEDROCK_MODEL = "us.amazon.nova-2-lite-v1:0"
BEDROCK_REGION = "ca-central-1"
MCP_CONNECT_TIMEOUT = 10
MCP_INIT_TIMEOUT = 10
MAX_TOKENS_PER_INCIDENT = 100_000
DEADLINE_BUFFER = 90
RECURSION_LIMIT = 12
PERMANENT_CATEGORIES = frozenset({"bedrock_auth", "unknown"})

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "")

SYSTEM_PROMPT = (
    "You are an AWS incident response diagnostician. You investigate Lambda function "
    "failures by querying real AWS infrastructure through diagnostic tools.\n\n"
    "RULES:\n"
    "1. ONLY use data returned by your tools. Never fabricate information.\n"
    "2. Reason step-by-step about the likely cause before choosing tools.\n"
    "3. Choose tools strategically based on the error type — don't call tools unnecessarily.\n"
    "4. After gathering enough evidence, call submit_diagnosis with your findings.\n"
    "5. If a tool returns an error or unexpected data, report it honestly.\n"
    "6. Report ALL detected faults in fault_types — the chaos script may inject multiple faults simultaneously.\n\n"
    "FAULT TYPES YOU MAY ENCOUNTER:\n"
    "- Permission loss: IAM policies revoked (S3, CloudWatch, or both)\n"
    "- Throttling: Reserved concurrency set to 0 or 1\n"
    "- Network block: Security group deny rules\n\n"
    "TOOL SELECTION GUIDANCE:\n"
    "- Access/permission errors (AccessDenied) → get_iam_state first, then logs\n"
    "- Throttling errors → get_lambda_config first, then logs\n"
    "- Unknown errors → get_recent_logs first for clues\n\n"
    "When you have enough evidence, call submit_diagnosis. For EVERY claim you make:\n"
    "- Provide an evidence pointer: which tool, which field, what value you observed, "
    "and your interpretation.\n"
    "- Each remediation step must reference evidence indices that justify it.\n"
    "- If you cannot point to specific tool output for a claim, note the gap in your "
    "evidence pointers."
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
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    incident: dict
    incident_id: str
    diagnosis: Diagnosis | None
    deadline: float
    token_usage: Annotated[list[TokenUsage], operator.add]


# ---------------------------------------------------------------------------
# Error classification (Split D)
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
    if isinstance(exc, McpInitError):
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
# Deadline check (Split E)
# ---------------------------------------------------------------------------

def check_deadline(state: AgentState, now: float | None = None) -> bool:
    """Return True if remaining time is under DEADLINE_BUFFER seconds."""
    if now is None:
        now = time.time()
    remaining = state["deadline"] - now
    return remaining < DEADLINE_BUFFER


# ---------------------------------------------------------------------------
# Validation helpers (Split F)
# ---------------------------------------------------------------------------

def validate_tool_args(tool_name: str, arguments: dict) -> dict:
    """Validate tool arguments via TOOL_ARG_SCHEMAS. Raises ValidationError or KeyError."""
    schema = TOOL_ARG_SCHEMAS[tool_name]
    validated = schema(**arguments)
    return validated.model_dump()


def validate_tool_response(tool_name: str, raw_json: str):
    """Validate tool response. Returns Pydantic model on success, error string on failure."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON response: {e}"

    schema = TOOL_RESPONSE_SCHEMAS.get(tool_name)
    if schema is None:
        return data

    try:
        return schema(**data)
    except ValidationError as e:
        return f"Response validation failed: {e}"


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

async def agent_reason(state: AgentState, llm) -> dict:
    """Call LLM with current messages. Checks deadline and token budget first."""
    messages = list(state["messages"])

    if check_deadline(state):
        messages.append(
            HumanMessage(
                content="Time is running out. Submit your diagnosis immediately "
                "with whatever evidence you have."
            )
        )

    total_tokens = sum(t.total_tokens for t in state.get("token_usage", []))
    if total_tokens >= MAX_TOKENS_PER_INCIDENT:
        messages.append(
            HumanMessage(content="Token budget exceeded. Submit your diagnosis immediately.")
        )

    response = await llm.ainvoke(messages)

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


async def execute_tools(state: AgentState, provider: ToolProvider) -> dict:
    """Validate args, call MCP tools, validate responses."""
    last_msg = state["messages"][-1]
    tool_messages = []

    for tc in last_msg.tool_calls:
        tool_name = tc["name"]
        arguments = tc["args"]
        tool_call_id = tc["id"]

        if tool_name == "submit_diagnosis":
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

def route_after_reason(state: AgentState) -> str:
    """Route after agent_reason: tools, submit, or end."""
    last_msg = state["messages"][-1]
    if not hasattr(last_msg, "tool_calls") or not last_msg.tool_calls:
        return "end"
    for tc in last_msg.tool_calls:
        if tc["name"] == "submit_diagnosis":
            return "submit"
    return "tools"


def extract_diagnosis(state: AgentState) -> dict:
    """Extract diagnosis from submit_diagnosis tool call."""
    last_msg = state["messages"][-1]
    for tc in last_msg.tool_calls:
        if tc["name"] == "submit_diagnosis":
            return {"diagnosis": Diagnosis(**tc["args"])}
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
            name="get_recent_logs",
            description="Get recent CloudWatch logs for a Lambda function.",
            func=_noop,
            args_schema=GetLogsArgs,
        ),
        StructuredTool(
            name="get_iam_state",
            description="Get IAM state for a Lambda function.",
            func=_noop,
            args_schema=GetIAMStateArgs,
        ),
        StructuredTool(
            name="get_lambda_config",
            description="Get Lambda configuration.",
            func=_noop,
            args_schema=GetLambdaConfigArgs,
        ),
        StructuredTool(
            name="submit_diagnosis",
            description="Submit your final diagnosis when you have enough evidence.",
            func=_noop,
            args_schema=Diagnosis,
        ),
    ]


def build_graph(tools: list[StructuredTool], provider: ToolProvider):
    """Build and compile the LangGraph diagnosis agent."""
    llm = ChatBedrockConverse(model=BEDROCK_MODEL, region_name=BEDROCK_REGION)
    llm_with_tools = llm.bind_tools(tools)

    async def _agent_reason(state: AgentState) -> dict:
        return await agent_reason(state, llm_with_tools)

    async def _execute_tools(state: AgentState) -> dict:
        return await execute_tools(state, provider)

    graph = StateGraph(AgentState)
    graph.add_node("agent_reason", _agent_reason)
    graph.add_node("execute_tools", _execute_tools)
    graph.add_node("extract_diagnosis", extract_diagnosis)

    graph.set_entry_point("agent_reason")
    graph.add_conditional_edges(
        "agent_reason",
        route_after_reason,
        {"tools": "execute_tools", "submit": "extract_diagnosis", "end": END},
    )
    graph.add_edge("execute_tools", "agent_reason")
    graph.add_edge("extract_diagnosis", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

async def _execute_agent(incident, incident_id, lambda_context, api_key):
    """Single attempt: connect MCP, build graph, invoke, return diagnosis."""
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
                        content=f"Investigate this incident:\n{json.dumps(incident, indent=2)}"
                    ),
                ],
                "incident": incident,
                "incident_id": incident_id,
                "diagnosis": None,
                "deadline": deadline,
                "token_usage": [],
            }

            result = await graph.ainvoke(
                initial_state, config={"recursion_limit": RECURSION_LIMIT}
            )
            return result.get("diagnosis")


async def run_agent(incident: dict, incident_id: str, lambda_context) -> Diagnosis | None:
    """Run the diagnosis agent with retry logic."""
    max_retries = 2
    last_error = None
    api_key = get_mcp_api_key()

    for attempt in range(max_retries):
        try:
            return await _execute_agent(incident, incident_id, lambda_context, api_key)
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
