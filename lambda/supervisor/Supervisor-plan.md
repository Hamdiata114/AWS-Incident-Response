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

TOOL_RESPONSE_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_recent_logs": LogsResponse,
    "get_iam_state": IAMStateResponse,
    "get_lambda_config": LambdaConfigResponse,
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
└── tests/
    ├── __init__.py
    ├── conftest.py            # Shared fixtures (moto DynamoDB, MockToolProvider)
    ├── test_orchestrator.py   # 44 tests
    ├── test_agent.py          # 30 tests
    └── test_schemas.py        # 34 tests
```

---

## 7. Key Implementation Details

### Function splits for single-responsibility testing

Six functions need splitting so each extracted helper tests exactly one behavior.

**Split A: `truncate_to_budget`** → extract 3 helpers:
- `_drop_oldest_logs(context, budget)` — pops oldest log events until under budget
- `_trim_iam_to_sids(context, budget)` — replaces inline policy docs with Sid lists
- `_drop_lambda_config(context, budget)` — replaces lambda_config with `{"dropped": True}`
- `truncate_to_budget` becomes thin orchestrator calling these 3 in order

**Split B: `gather_context`** → extract 2 helpers:
- `_call_mcp_tools(session, lambda_name, incident_id)` — calls 3 MCP tools, returns `(context, raw_sizes)`
- `_compute_metrics(raw_sizes, token_budget, final_tokens, truncation_details)` — pure function, builds metrics dict
- `gather_context` opens MCP session, delegates to helpers

**Split C: `handler`** → extract 2 helpers:
- `_dedup_or_recover(incident_id)` — returns `"skip"` if already handled, `None` if proceed. Handles crash recovery + stale re-entry.
- `_store_context(incident_id, incident, context)` — DynamoDB put to incident-context table
- `handler` becomes thin coordinator

**Split D: `run_agent`** → extract classifier:
- `classify_error(exception)` — pure function mapping exception → AgentError with category
- `run_agent` retry loop calls `classify_error`

**Split E: `agent_reason`** → extract deadline check:
- `check_deadline(state, now=None)` — returns True if remaining < 90s. Accepts `now` param for testability.
- `agent_reason` calls `check_deadline`, then LLM

**Split F: `execute_tools`** → extract validators:
- `validate_tool_args(tool_name, arguments)` — validates via `TOOL_ARG_SCHEMAS`, returns validated dict or raises ValidationError
- `validate_tool_response(tool_name, raw_json)` — validates via `TOOL_RESPONSE_SCHEMAS`, returns model or error string
- `execute_tools` orchestrates: validate args → call provider → validate response

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

Each code step includes its unit tests. **Do not proceed to the next step until the gate passes.**

### Step 1 — Project setup
| What | Files |
|------|-------|
| Update `requirements.txt` (add langgraph, langchain-aws, pydantic) | `requirements.txt` |
| Install dev deps locally: `pip install pytest moto boto3 pydantic` | — |
| Create `tests/__init__.py` + `tests/conftest.py` (shared fixtures) | `tests/` |

**Gate:** `pytest tests/ --collect-only` runs without import errors.

### Step 2 — schemas.py + test_schemas.py
| What | Files |
|------|-------|
| Create `schemas.py` — Pydantic models, `TOOL_ARG_SCHEMAS`, `TOOL_RESPONSE_SCHEMAS`, `ToolProvider` protocol, `McpToolProvider`, `MockToolProvider`, `AgentError` | `schemas.py` |
| Create `tests/test_schemas.py` — 34 tests (see §13) | `tests/test_schemas.py` |

**Gate:** `pytest tests/test_schemas.py -v` — 34/34 pass.

### Step 3 — agent.py + test_agent.py
| What | Files |
|------|-------|
| Create `agent.py` — graph, tools, prompt, `run_agent()` with retry, `classify_error`, `check_deadline`, `validate_tool_args`, `validate_tool_response`, `execute_tools`, `agent_reason`, `create_tools`, `build_graph`, `get_mcp_api_key` (splits D, E, F) | `agent.py` |
| Create `tests/test_agent.py` — 30 tests (see §13) | `tests/test_agent.py` |

**Gate:** `pytest tests/test_agent.py -v` — 30/30 pass.

### Step 4 — orchestrator.py + test_orchestrator.py
| What | Files |
|------|-------|
| Update `orchestrator.py` — simplify handler, call agent, ERROR state, `error_category`. Extract `_dedup_or_recover`, `_store_context`, `_drop_oldest_logs`, `_trim_iam_to_sids`, `_drop_lambda_config`, `_compute_metrics` (splits A, B, C) | `orchestrator.py` |
| Create `tests/test_orchestrator.py` — 44 tests (see §13) | `tests/test_orchestrator.py` |

**Gate:** `pytest tests/test_orchestrator.py -v` — 44/44 pass.

### Step 5 — Full suite gate
| What | Files |
|------|-------|
| Run full suite to catch cross-module regressions | all |

**Gate:** `pytest tests/ -v` — 108/108 pass.

### Step 6 — AWS infra
| What | Tool |
|------|------|
| Grant Bedrock `InvokeModel` permission to Lambda role | AWS CLI |
| Create SSM parameter `/incident-response/mcp-api-key` (SecureString) + grant `ssm:GetParameter` | AWS CLI |
| Update Lambda timeout to 300s | AWS CLI |

### Step 7 — Build & deploy
| What | Tool |
|------|------|
| Build deps on EC2 (Docker), deploy ZIP | EC2 + AWS CLI |
| Deploy `incident-watchdog` Lambda + EventBridge rule | AWS CLI |

### Step 8 — Integration test
| What | Tool |
|------|------|
| Chaos inject → trigger → verify diagnosis in DynamoDB (see §9) | chaos script |

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

## 13. Unit Tests

**Constraint:** Each test tests exactly one behavior. Functions with multiple behaviors are split first (see §7 splits A–F).

### Infrastructure

- **DynamoDB**: `moto` mock for all state management functions
- **MCP**: `MockToolProvider` (defined in schemas.py) for tool calls
- **Bedrock/SSM**: `unittest.mock.patch` on boto3 clients
- **Time**: `unittest.mock.patch("time.time")` for deadline tests
- **conftest.py**: Shared fixtures for DynamoDB table creation, sample incidents, MockToolProvider instances

### test_orchestrator.py (44 tests)

#### `get_state`
| Test | Scenario |
|------|----------|
| `test_get_state_returns_item_when_exists` | Item in table → returns deserialized dict |
| `test_get_state_returns_none_when_missing` | Missing key → None |
| `test_get_state_handles_numeric_attribute` | N-type attribute returns string of number |

#### `write_initial_state`
| Test | Scenario |
|------|----------|
| `test_write_initial_state_creates_item` | New ID → RECEIVED status + timestamps + 7-day TTL |
| `test_write_initial_state_rejects_duplicate` | Duplicate ID → ConditionalCheckFailedException |

#### `touch_updated_at`
| Test | Scenario |
|------|----------|
| `test_touch_updated_at_updates_timestamp` | updated_at changes to current UTC |

#### `transition_state`
| Test | Scenario |
|------|----------|
| `test_transition_state_updates_status` | from_status matches → status changes |
| `test_transition_state_fails_on_wrong_status` | from_status mismatch → ConditionalCheckFailedException |
| `test_transition_state_stores_error_reason` | error_reason persisted |
| `test_transition_state_truncates_error_reason_to_500` | 600-char reason → 500 chars stored |
| `test_transition_state_stores_error_category` | error_category persisted |
| `test_transition_state_omits_error_fields_when_none` | Both None → no error attributes on item |

#### `estimate_tokens`
| Test | Scenario |
|------|----------|
| `test_estimate_tokens_empty_dict` | `{}` → 0 |
| `test_estimate_tokens_small_payload` | Known JSON → len//4 |
| `test_estimate_tokens_datetime_default_str` | datetime in data → serializes without error |
| `test_estimate_tokens_nested_structure` | Nested dicts/lists → correct estimate |

#### `_drop_oldest_logs`
| Test | Scenario |
|------|----------|
| `test_drop_oldest_logs_removes_until_under_budget` | Drops oldest events one by one |
| `test_drop_oldest_logs_no_events_key` | Missing `events` → empty details |
| `test_drop_oldest_logs_empty_events_list` | `[]` → zero dropped |
| `test_drop_oldest_logs_already_under_budget` | Under budget → no events dropped |
| `test_drop_oldest_logs_drains_all_events` | All removed if still over after full drain |
| `test_drop_oldest_logs_no_cloudwatch_key` | Missing `cloudwatch_logs` → empty details |
| `test_drop_oldest_logs_non_dict_data` | Non-dict logs_data → untouched |

#### `_trim_iam_to_sids`
| Test | Scenario |
|------|----------|
| `test_trim_iam_replaces_with_sids` | Full policy → StatementSids list |
| `test_trim_iam_unnamed_sid` | Missing Sid → "unnamed" |
| `test_trim_iam_no_iam_key` | Missing `iam_policy` → empty details |
| `test_trim_iam_already_under_budget` | Under budget → no trimming |
| `test_trim_iam_non_dict_policy` | Non-dict iam_data → untouched |
| `test_trim_iam_no_statement_key` | Policy without "Statement" → untouched |

#### `_drop_lambda_config`
| Test | Scenario |
|------|----------|
| `test_drop_config_replaces_with_flag` | Replaced with `{"dropped": True}` |
| `test_drop_config_no_key` | Missing key → empty details |
| `test_drop_config_already_under_budget` | Under budget → no action |

#### `truncate_to_budget`
| Test | Scenario |
|------|----------|
| `test_truncate_zero_budget_returns_skipped` | budget≤0 → skipped details |
| `test_truncate_under_budget_no_changes` | Already under → unchanged |
| `test_truncate_applies_stages_in_order` | Logs→IAM→config ordering verified |

#### `_compute_metrics`
| Test | Scenario |
|------|----------|
| `test_compute_metrics_basic` | Correct totals, truncated flag, details |
| `test_compute_metrics_no_truncation` | raw≤budget → truncated=False |
| `test_compute_metrics_zero_budget` | budget=0 → truncated=False |

#### `parse_sns_event`
| Test | Scenario |
|------|----------|
| `test_parse_valid_sns` | Extracts + JSON-parses message |
| `test_parse_missing_records` | KeyError |
| `test_parse_invalid_json_body` | JSONDecodeError |
| `test_parse_empty_records` | IndexError |

#### `_dedup_or_recover`
| Test | Scenario |
|------|----------|
| `test_dedup_new_returns_none` | No state → writes initial, returns None |
| `test_dedup_received_returns_none` | RECEIVED → crash recovery, returns None |
| `test_dedup_stale_investigating_returns_none` | INVESTIGATING + stale → resets, returns None |
| `test_dedup_active_investigating_returns_skip` | INVESTIGATING + fresh → "skip" |
| `test_dedup_terminal_returns_skip` | DIAGNOSED/FAILED → "skip" |

#### `_store_context`
| Test | Scenario |
|------|----------|
| `test_store_context_writes_item` | Correct fields + TTL in DynamoDB |
| `test_store_context_defaults_error_type` | Missing error_type → "unknown" |

#### `handler`
| Test | Scenario |
|------|----------|
| `test_handler_happy_path` | Full flow → CONTEXT_GATHERED, returns 200 |
| `test_handler_skips_duplicate` | Already handled → 200 "already handled" |
| `test_handler_failed_on_exception` | gather raises → FAILED, re-raises |
| `test_handler_logs_transition_failure` | transition_state fails → logged, original re-raised |

### test_agent.py (30 tests)

#### `classify_error`
| Test | Scenario |
|------|----------|
| `test_classify_timeout` | TimeoutError → mcp_connection |
| `test_classify_connection_error` | ConnectionError → mcp_connection |
| `test_classify_os_error` | OSError → mcp_connection |
| `test_classify_access_denied` | ClientError AccessDeniedException → bedrock_auth |
| `test_classify_unauthorized` | ClientError UnauthorizedException → bedrock_auth |
| `test_classify_throttling` | ClientError ThrottlingException → bedrock_transient |
| `test_classify_service_unavailable` | ClientError ServiceUnavailableException → bedrock_transient |
| `test_classify_model_timeout` | ClientError ModelTimeoutException → bedrock_transient |
| `test_classify_unknown_client_error` | ClientError other code → unknown |
| `test_classify_mcp_init` | McpInitError → mcp_init |

#### `run_agent`
| Test | Scenario |
|------|----------|
| `test_run_agent_success` | Happy path → Diagnosis |
| `test_run_agent_retries_mcp_connection` | ConnectionError then success |
| `test_run_agent_retries_bedrock_transient` | ThrottlingException then success |
| `test_run_agent_no_retry_bedrock_auth` | AccessDeniedException → raises immediately |
| `test_run_agent_raises_after_max_retries` | 2 failures → raises last AgentError |
| `test_run_agent_backoff_timing` | asyncio.sleep called with 2^attempt |
| `test_run_agent_returns_none_no_diagnosis` | diagnosis=None in result → returns None |

#### `check_deadline`
| Test | Scenario |
|------|----------|
| `test_check_deadline_under_90s` | remaining=89 → True |
| `test_check_deadline_over_90s` | remaining=91 → False |
| `test_check_deadline_exactly_90s` | remaining=90 → False (not strictly less) |

#### `agent_reason`
| Test | Scenario |
|------|----------|
| `test_agent_reason_calls_bedrock` | Time available → calls LLM, returns response |
| `test_agent_reason_forces_diagnosis_near_deadline` | <90s → injects "submit now" message |
| `test_agent_reason_extracts_token_usage` | Token usage appended to state |

#### `validate_tool_args`
| Test | Scenario |
|------|----------|
| `test_validate_args_valid` | Valid args → returns validated dict |
| `test_validate_args_missing_field` | Missing lambda_name → ValidationError |
| `test_validate_args_extra_fields` | Extra fields ignored |
| `test_validate_args_unknown_tool` | Unknown tool_name → KeyError |

#### `validate_tool_response`
| Test | Scenario |
|------|----------|
| `test_validate_response_valid_logs` | Valid JSON → LogsResponse model |
| `test_validate_response_invalid_json` | Non-JSON → error string |
| `test_validate_response_missing_field` | Missing required field → error string |
| `test_validate_response_with_error_field` | `"error"` key passes validation |

#### `execute_tools`
| Test | Scenario |
|------|----------|
| `test_execute_tools_valid` | Valid args + response → result in messages |
| `test_execute_tools_invalid_args_no_mcp` | Bad args → error, provider never called |
| `test_execute_tools_invalid_response` | Bad MCP response → validation error returned |

#### `create_tools`
| Test | Scenario |
|------|----------|
| `test_create_tools_returns_four` | List length = 4 |
| `test_create_tools_has_submit_diagnosis` | One tool named submit_diagnosis |
| `test_create_tools_has_mcp_tools` | Contains all 3 MCP tool names |

#### `build_graph`
| Test | Scenario |
|------|----------|
| `test_build_graph_compiles` | Returns compiled graph |
| `test_build_graph_recursion_limit` | recursion_limit=12 |

#### `get_mcp_api_key`
| Test | Scenario |
|------|----------|
| `test_get_mcp_api_key_returns_value` | Mocked SSM → decrypted value |
| `test_get_mcp_api_key_missing_param` | ParameterNotFound → raises |

### test_schemas.py (34 tests)

#### Pydantic models
| Test | Scenario |
|------|----------|
| `test_log_event_valid` | Valid fields → ok |
| `test_log_event_missing_timestamp` | ValidationError |
| `test_log_event_missing_message` | ValidationError |
| `test_logs_response_valid` | Valid fields → ok |
| `test_logs_response_empty_events` | `[]` → ok |
| `test_logs_response_with_error` | Optional error field → ok |
| `test_logs_response_missing_log_group` | ValidationError |
| `test_iam_state_valid` | Valid fields → ok |
| `test_iam_state_empty_policies` | Empty dict + list → ok |
| `test_iam_state_missing_role_name` | ValidationError |
| `test_lambda_config_full` | All fields → ok |
| `test_lambda_config_minimal` | Only FunctionName → ok |
| `test_lambda_config_missing_function_name` | ValidationError |
| `test_lambda_config_zero_concurrency` | ReservedConcurrentExecutions=0 → ok |
| `test_evidence_pointer_valid` | All 4 strings → ok |
| `test_evidence_pointer_missing_tool` | ValidationError |
| `test_remediation_step_valid` | All fields → ok |
| `test_remediation_step_empty_evidence_basis` | `[]` → ok |
| `test_remediation_step_missing_risk_level` | ValidationError |
| `test_diagnosis_valid` | Full valid → ok |
| `test_diagnosis_empty_fault_types` | `[]` → ok |
| `test_diagnosis_missing_root_cause` | ValidationError |
| `test_token_usage_valid` | 3 ints → ok |
| `test_token_usage_missing_field` | ValidationError |
| `test_get_logs_args_valid` | lambda_name → ok |
| `test_get_logs_args_missing` | ValidationError |
| `test_get_iam_args_valid` | lambda_name → ok |
| `test_get_iam_args_missing` | ValidationError |
| `test_get_config_args_valid` | lambda_name → ok |
| `test_get_config_args_missing` | ValidationError |

#### `TOOL_ARG_SCHEMAS`
| Test | Scenario |
|------|----------|
| `test_tool_arg_schemas_three_entries` | len = 3 |
| `test_tool_arg_schemas_correct_keys` | Expected tool names |

#### `McpToolProvider` / `MockToolProvider` / `AgentError`
| Test | Scenario |
|------|----------|
| `test_mcp_provider_returns_text` | Delegates to session, returns content[0].text |
| `test_mcp_provider_empty_returns_error` | Empty content → error JSON |
| `test_mock_provider_known_tool` | Returns canned response |
| `test_mock_provider_unknown_tool` | Returns error JSON |
| `test_agent_error_stores_fields` | .category and .message accessible |
| `test_agent_error_str_format` | `"[cat] msg"` |
| `test_agent_error_is_exception` | isinstance check |

### Verification

1. All 108 tests listed — one behavior per test
2. Every function in orchestrator.py, agent.py, schemas.py has tests
3. 6 function splits documented (§7) — no multi-concern functions remain
4. `TOOL_RESPONSE_SCHEMAS` dict added to §3
5. Run: `cd lambda/supervisor && python -m pytest tests/ -v`

### Resolved Decisions

1. `_dedup_or_recover` returns plain string (`"skip"` or `None`).
2. `check_deadline(state, now=None)` accepts explicit `now` param, defaults to `time.time()`.

---

## Unresolved Questions

1. Exact `langchain-aws` version to pin? (needs testing against LangGraph 1.0.8)
2. Should `MockToolProvider` live in `schemas.py` or a separate `testing.py`?
3. SSM parameter: create via CloudFormation or manual CLI?
