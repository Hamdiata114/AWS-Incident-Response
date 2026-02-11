# Phase 3: LangGraph Supervisor Agent

## Overview

Transform `lambda/supervisor/orchestrator.py` from a procedural script into a LangGraph ReAct agent that reasons about which MCP tools to call, validates results, and produces a structured diagnosis + remediation plan.

**Decisions:** Bedrock Claude (`anthropic.claude-3-5-sonnet-20241022-v2:0`), 5-min Lambda timeout, agent decides which tools to call.

---

## 1. Graph Architecture

```
handler (SNS parse, dedup, DynamoDB)
  └─ async run_agent()
       └─ MCP SSE session opened
            └─ LangGraph graph.ainvoke()

Graph:
  START → agent_reason ──┐
            ▲             │
            │      [conditional edge]
            │        ├── tool_call → execute_tools → agent_reason
            │        └── submit_diagnosis called → END
            └─────────────────┘
```

- **`agent_reason`**: Calls Bedrock Claude with messages + tool schemas. LLM either calls a tool or calls `submit_diagnosis`.
- **`execute_tools`**: Validates LLM-provided args through `TOOL_ARG_SCHEMAS` before calling MCP. On `ValidationError`, returns the error to the agent (no MCP call). Otherwise runs MCP tool via SSE, validates response with Pydantic, appends result to messages.
- **Termination**: Agent calls `submit_diagnosis` tool with structured output → graph ends.
- **Safety**: `recursion_limit=12` (max 5-6 tool calls), timeout watchdog with 90s buffer (60s worst-case LLM latency + 30s safety margin) checks remaining Lambda time before each LLM call.

---

## 2. Tools (4 total)

### MCP Tools (wrapped with validation)
| Tool | When to use | Validation |
|------|-------------|------------|
| `get_recent_logs(lambda_name)` | Always useful; start here for general errors | `LogsResponse` schema |
| `get_iam_state(lambda_name)` | Permission/access errors (AccessDenied, etc.) | `IAMStateResponse` schema |
| `get_lambda_config(lambda_name)` | Throttling, concurrency, runtime issues | `LambdaConfigResponse` schema |

### Terminal Tool
| Tool | Purpose |
|------|---------|
| `submit_diagnosis(...)` | Agent calls this when done. Args = structured diagnosis. Ends the graph. |

Tool wrappers accept a `ToolProvider` protocol — graph is built independently of the MCP transport.

**Empty response guard**: Every tool wrapper must check for empty responses (`if not result.content`) and return `{"error": "Tool returned empty response"}` instead of indexing `content[0]`. This is handled inside `McpToolProvider.call_tool()`.

---

## 3. Validation Schemas (Pydantic)

```python
# Tool response validation
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

# Evidence traceability — every claim/decision points back to a tool result
class EvidencePointer(BaseModel):
    tool: str              # "get_iam_state" | "get_recent_logs" | "get_lambda_config"
    field: str             # JSONPath-like: "inline_policies.data-processor-access.Statement"
    value: str             # The actual observed value (quoted from tool output)
    interpretation: str    # What the agent concluded from this

# Diagnosis output (submit_diagnosis argument schema)
class RemediationStep(BaseModel):
    action: str          # "Restore S3 IAM policy"
    details: str         # Specifics
    evidence_basis: list[int]  # Indices into Diagnosis.evidence that justify this step
    risk_level: str      # low | medium | high
    requires_approval: bool

class Diagnosis(BaseModel):
    root_cause: str
    fault_types: list[str]  # permission_loss | throttling | network_block | unknown
    affected_resources: list[str]
    severity: str        # critical | high | medium | low
    evidence: list[EvidencePointer]  # Structured pointers to tool results
    remediation_plan: list[RemediationStep]  # Each step references evidence by index
```

### ToolProvider protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class ToolProvider(Protocol):
    async def call_tool(self, name: str, arguments: dict) -> str:
        """Call a tool by name and return the raw JSON string."""
        ...

class McpToolProvider:
    """Production implementation — delegates to an MCP ClientSession."""
    def __init__(self, session: ClientSession):
        self._session = session

    async def call_tool(self, name: str, arguments: dict) -> str:
        result = await self._session.call_tool(name, arguments)
        if not result.content:
            return '{"error": "Tool returned empty response"}'
        return result.content[0].text

class MockToolProvider:
    """Test implementation — returns canned responses."""
    def __init__(self, responses: dict[str, str]):
        self._responses = responses

    async def call_tool(self, name: str, arguments: dict) -> str:
        return self._responses.get(name, '{"error": "unknown tool"}')
```

### Tool argument validation

```python
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
```

**Hallucination prevention**: If Pydantic validation fails on a tool response, the tool returns a validation error message to the agent (not the raw data). The agent sees the error and can retry or work with what it has.

---

## 4. State Schema

```python
class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    incident: dict              # Original SNS payload
    incident_id: str
    diagnosis: Diagnosis | None # Set when submit_diagnosis called
    deadline: float             # time.time() deadline for timeout watchdog
    token_usage: list[TokenUsage]  # Accumulated per-LLM-call usage from Bedrock response
```

---

## 5. System Prompt

```
You are an AWS incident response diagnostician. You investigate Lambda function failures by querying real AWS infrastructure through diagnostic tools.

RULES:
1. ONLY use data returned by your tools. Never fabricate information.
2. Reason step-by-step about the likely cause before choosing tools.
3. Choose tools strategically based on the error type — don't call tools unnecessarily.
4. After gathering enough evidence, call submit_diagnosis with your findings.
5. If a tool returns an error or unexpected data, report it honestly.
6. Report ALL detected faults in fault_types — the chaos script may inject multiple faults simultaneously.

FAULT TYPES YOU MAY ENCOUNTER:
- Permission loss: IAM policies revoked (S3, CloudWatch, or both)
- Throttling: Reserved concurrency set to 0 or 1
- Network block: Security group deny rules

TOOL SELECTION GUIDANCE:
- Access/permission errors (AccessDenied) → get_iam_state first, then logs
- Throttling errors → get_lambda_config first, then logs
- Unknown errors → get_recent_logs first for clues

When you have enough evidence, call submit_diagnosis. For EVERY claim you make:
- Provide an evidence pointer: which tool, which field, what value you observed, and your interpretation.
- Each remediation step must reference evidence indices that justify it.
- If you cannot point to specific tool output for a claim, note the gap in your evidence pointers.
```

---

## 6. File Structure

```
lambda/supervisor/
├── orchestrator.py      # Lambda handler (SNS parse, dedup, DynamoDB, calls run_agent)
├── agent.py             # LangGraph graph definition, tool wrappers, prompt
├── schemas.py           # Pydantic models (validation + Diagnosis output) + ToolProvider protocol
├── requirements.txt     # + langgraph, langchain-aws, pydantic
```

---

## 7. Key Implementation Details

### orchestrator.py changes
- Replace `asyncio.run()` with `asyncio.new_event_loop()` + `loop.run_until_complete()` + `loop.close()` to avoid "cannot run nested event loop" errors if Lambda reuses an existing loop.
- Keep: SNS parsing, DynamoDB state management, dedup (with stale re-entry logic), error handling
- Remove: `gather_context()`, `truncate_to_budget()`, `estimate_tokens()`
- Add: Call `run_agent(incident, incident_id, lambda_context)` instead
- Add: After `run_agent()` returns, check if diagnosis is `None`. If so, transition to `FAILED` with `error_reason="recursion limit exhausted without diagnosis"`.
- New states: RECEIVED → INVESTIGATING → DIAGNOSED | FAILED | ERROR (replace CONTEXT_GATHERED)
  - **FAILED** = agent ran but couldn't diagnose (recursion limit, timeout, circuit breaker)
  - **ERROR** = infra/external failure prevented the agent from running (MCP down, bad API key, etc.)
- Store in `incident-context` table:
  - `diagnosis`: structured Diagnosis JSON
  - `reasoning_chain`: full list of LLM messages (tool calls, results, reasoning) for auditability
    - **400KB guard**: Before writing, check `len(json.dumps(reasoning_chain).encode()) > 350_000`. If exceeded, truncate oldest messages (keep system prompt + last 3 turns) and set `"truncated": true` on the item. DynamoDB max item size is 400KB.

### agent.py structure
```python
async def run_agent(incident, incident_id, lambda_context) -> Diagnosis:
    headers = {"Authorization": f"Bearer {MCP_API_KEY}"}
    async with sse_client(MCP_SERVER_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            provider = McpToolProvider(session)
            tools = create_tools(provider)       # MCP wrappers + submit_diagnosis
            graph = build_graph(tools)           # Compile LangGraph
            result = await graph.ainvoke(initial_state)
            return result["diagnosis"]
```

### Timeout watchdog
Before each LLM call in `agent_reason`, check:
```python
# 90s buffer = 60s worst-case LLM latency + 30s safety margin
remaining = state["deadline"] - time.time()
if remaining < 90:
    # Force diagnosis with whatever evidence we have
    # Inject "time running out, submit diagnosis now" message
```

### Token observability
- After each Bedrock Claude call, extract `response.usage` (prompt_tokens, completion_tokens, total_tokens)
- Append to `state["token_usage"]` list — one entry per LLM call
- At end of run, log aggregated metrics to CloudWatch:
  ```json
  {"event": "agent_metrics", "incident_id": "...", "llm_calls": 3,
   "total_prompt_tokens": 4200, "total_completion_tokens": 800,
   "total_tokens": 5000, "budget_remaining": 595000}
  ```
- Also store in `incident-context` DynamoDB item alongside diagnosis + reasoning chain
- **CloudWatch reasoning summary**: At end of run, log a structured summary to CloudWatch so debugging doesn't require DynamoDB queries:
  ```json
  {"event": "agent_reasoning_summary", "incident_id": "...",
   "tools_called": ["get_recent_logs", "get_iam_state"],
   "fault_types": ["permission_loss"], "root_cause": "S3 policy revoked",
   "severity": "high", "steps": 3}
  ```
  Full reasoning chain stays in DynamoDB only (too large for CloudWatch).

### Environment variables
- Bedrock auth uses Lambda IAM role — no API key needed
- `MCP_SERVER_URL` — existing (plaintext env var)
- `MCP_API_KEY` — fetched from SSM Parameter Store (`/incident-response/mcp-api-key`, SecureString) at cold start instead of plaintext env var
- Remove `TOKEN_BUDGET` (no longer needed; LLM manages its own context)

### SSM secret fetch

```python
ssm = boto3.client("ssm", region_name="ca-central-1")

def get_mcp_api_key() -> str:
    resp = ssm.get_parameter(Name="/incident-response/mcp-api-key", WithDecryption=True)
    return resp["Parameter"]["Value"]

MCP_API_KEY = get_mcp_api_key()  # Module-level: runs once at cold start
```

### Cost guards

**Per-incident cap**: Track cumulative `total_tokens` across LLM calls in `state["token_usage"]`. Before each Bedrock call, sum usage so far — if it exceeds `MAX_TOKENS_PER_INCIDENT` (default 100,000), force the agent to submit diagnosis with current evidence. Prevents runaway single-incident costs.

**Time-window circuit breaker**: Before `run_agent`, query `incident-state` for incidents created in the last hour (`created_at > now - 1h`). If count exceeds `MAX_INCIDENTS_PER_HOUR` (default 20), skip the LLM agent entirely — transition to `FAILED` with `error_reason="circuit breaker: too many incidents in window"` and log a CloudWatch alarm metric. Prevents cost spikes from chaos script loops or SNS retry storms.

### Scope boundary
`DIAGNOSED` is intentionally terminal for Phase 3. The Supervisor diagnoses faults but does not remediate them. Handoff to Resolver/Critic agents is Phase 4+ scope (see Section 12).

### run_agent robustness

`run_agent` wraps MCP connection + LangGraph invocation with categorized error handling and retry.

**AgentError** (in `schemas.py`):
```python
class AgentError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(f"[{category}] {message}")
```

**Error categories:**
| Category | Trigger | Retry? |
|----------|---------|--------|
| `mcp_connection` | SSE connect timeout, `ConnectionError`, `OSError` | Yes |
| `mcp_init` | MCP handshake (`initialize()`) failure | Yes |
| `bedrock_auth` | `ClientError` with `AccessDeniedException` / `UnauthorizedException` | No (permanent) |
| `bedrock_transient` | `ClientError` with `ThrottlingException` / `ServiceUnavailableException` / `ModelTimeoutException` | Yes |
| `unknown` | Unclassified `Exception` (caught in orchestrator) | No |

**Retry logic:**
- `max_retries = 2` (1 original + 1 retry)
- Exponential backoff: `2^attempt` seconds between retries
- Permanent errors (`bedrock_auth`) skip retries and raise immediately

**Timeouts:**
- `MCP_CONNECT_TIMEOUT = 10s` — wraps SSE connection via `asyncio.timeout()`
- `MCP_INIT_TIMEOUT = 10s` — wraps MCP `session.initialize()`
- Timeout → classified as `mcp_connection`, retried

**run_agent structure** (in `agent.py`):
```python
MCP_CONNECT_TIMEOUT = 10
MCP_INIT_TIMEOUT = 10

async def run_agent(incident, incident_id, lambda_context) -> Diagnosis:
    max_retries = 2
    last_error = None

    for attempt in range(max_retries):
        try:
            headers = {"Authorization": f"Bearer {MCP_API_KEY}"}
            async with asyncio.timeout(MCP_CONNECT_TIMEOUT):
                sse_cm = sse_client(MCP_SERVER_URL, headers=headers)
                read, write = await sse_cm.__aenter__()

            async with sse_cm:
                async with ClientSession(read, write) as session:
                    async with asyncio.timeout(MCP_INIT_TIMEOUT):
                        await session.initialize()
                    provider = McpToolProvider(session)
                    tools = create_tools(provider)
                    graph = build_graph(tools)
                    result = await graph.ainvoke(initial_state)
                    return result["diagnosis"]

        except asyncio.TimeoutError as e:
            last_error = AgentError("mcp_connection", f"MCP timeout: {e}")
        except (ConnectionError, OSError) as e:
            last_error = AgentError("mcp_connection", str(e))
        except McpInitError as e:
            last_error = AgentError("mcp_init", str(e))
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDeniedException", "UnauthorizedException"):
                raise AgentError("bedrock_auth", str(e))
            elif code in ("ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"):
                last_error = AgentError("bedrock_transient", str(e))
            else:
                raise AgentError("unknown", str(e))

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    raise last_error
```

**Orchestrator handler** (in `orchestrator.py`):
```python
try:
    diagnosis = await run_agent(incident, incident_id, lambda_context)
    if diagnosis is None:
        transition_state(..., "FAILED", error_reason="recursion limit exhausted")
    else:
        transition_state(..., "DIAGNOSED")
except AgentError as e:
    transition_state(..., "ERROR", error_reason=e.message, error_category=e.category)
except Exception as e:
    transition_state(..., "ERROR", error_reason=str(e), error_category="unknown")
```

**DynamoDB fields** stored on ERROR:
- `error_reason` — truncated to 500 chars
- `error_category` — one of the categories above

---

## 8. Implementation Steps

| Step | What | Files |
|------|------|-------|
| 1 | Create `schemas.py` — Pydantic models + `AgentError` | `schemas.py` |
| 2 | Create `agent.py` — graph, tools, prompt, `run_agent()` with retry/error classification | `agent.py` |
| 3 | Update `orchestrator.py` — simplify handler, call agent, ERROR state, `error_category` | `orchestrator.py` |
| 4 | Update `requirements.txt` | `requirements.txt` |
| 5 | Grant Bedrock `InvokeModel` permission to Lambda role | AWS CLI |
| 5b | Create SSM parameter `/incident-response/mcp-api-key` (SecureString) + grant Lambda role `ssm:GetParameter` | AWS CLI |
| 6 | Update Lambda timeout to 300s | AWS CLI |
| 7 | Build deps on EC2 (Docker), deploy ZIP | EC2 + AWS CLI |
| 8 | Deploy `incident-watchdog` Lambda + EventBridge rule | AWS CLI |
| 9 | Test: chaos inject → trigger → verify diagnosis in DynamoDB | chaos script |

---

## 9. Verification

1. `chaos/iam_chaos.py revoke --target s3` → invoke data-processor → SNS → supervisor
2. Check `incident-state`: status = `DIAGNOSED`
3. Check `incident-context`: `diagnosis` field contains structured JSON with:
   - `fault_types: ["permission_loss"]`
   - `root_cause` mentions S3 policy revoked
   - `evidence` cites IAM tool results with structured pointers
   - `remediation_plan` includes "restore policy" step with evidence_basis indices
4. Check CloudWatch logs: agent reasoning chain visible (tool selection, validation, diagnosis)
5. Repeat with `revoke --target cloudwatch` and `revoke --target both`
6. **MCP down**: Stop MCP server → trigger → verify `ERROR` with `error_category=mcp_connection`
7. **Bad Bedrock auth**: Remove Bedrock `InvokeModel` IAM permission → trigger → verify `ERROR` with `error_category=bedrock_auth` (no retry)
8. **MCP timeout**: Point at black-hole IP → verify timeout after 10s, 1 retry, then `ERROR`
9. **Retry recovery**: Block MCP port, unblock after 2s → verify agent succeeds on retry

---

## 10. New Dependencies

```
langgraph==1.0.8
langchain-aws==0.2.9
langchain-core==1.2.9
pydantic==2.12.5
```

**Cold start note**: Adding these deps increases cold start time. The 5-minute timeout already accounts for worst-case cold start + LLM latency. Mitigations if needed: Lambda layers, provisioned concurrency.

---

## 11. Stale Incident Watchdog

A lightweight Lambda (`incident-watchdog`) triggered every 5 min by EventBridge to clean up incidents stuck in `INVESTIGATING` after a Lambda crash.

**Logic:**
1. Scan `incident-state` for `status=INVESTIGATING` AND `updated_at` older than 10 min (no need to scan `ERROR` — it's terminal)
2. Transition each to `FAILED` with `error_reason="stale watchdog timeout"`
3. Log transitions to CloudWatch

| Resource | Detail |
|----------|--------|
| Lambda | `incident-watchdog` |
| EventBridge rule | `incident-stale-check` (rate: 5 min) |
| Region | `ca-central-1` |

**File:** `lambda/watchdog/handler.py`

---

## 12. Future Work (Phase 4+)

- **`DIAGNOSED → REMEDIATING` transition**: Supervisor hands diagnosis to Resolution Agent.
- **New states**: `REMEDIATING`, `AWAITING_APPROVAL`, `REMEDIATED`.
- **Resolution Agent**: Proposes and executes remediation steps from the diagnosis.
- **Critic Agent**: Reviews proposed actions; triggers SNS human-approval gate for critical/high-risk steps.
- **Communication**: All Resolver ↔ Critic interaction mediated by Supervisor (never direct).

---

## Unresolved Questions

1. Exact `langchain-aws` version to pin? (needs testing against LangGraph 1.0.8)
2. Should `MockToolProvider` live in `schemas.py` or a separate `testing.py`?
3. SSM parameter: create via CloudFormation or manual CLI?
