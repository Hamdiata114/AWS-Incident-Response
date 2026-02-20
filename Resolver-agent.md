# Resolver Agent — Implementation Plan

## Context

The supervisor agent diagnoses faults and produces a `Diagnosis` with high-level `RemediationStep`s. We need a resolver agent that takes that diagnosis and produces **concrete, executable remediation proposals** with exact AWS parameters. The resolver is a separate Lambda invoked synchronously by the supervisor, with its own MCP server for remediation-lookup tools.

## Scope

- Shared package (`lambda/shared/`) to eliminate code duplication between supervisor and resolver
- Resolver Lambda + LangGraph agent (imports from shared)
- Resolver MCP server with 2 remediation-lookup tools (IAM + concurrency; SG skipped for now)
- Supervisor integration (invoke resolver after diagnosis, with timeout/failure handling)
- Critic approval is **out of scope** — resolver only proposes
- Execution-time partial failure handling is deferred to the Critic agent

---

## 1. Shared Package (`lambda/shared/`)

Extract duplicated code into a shared package both supervisor and resolver import from.

### `lambda/shared/__init__.py`
Empty init.

### `lambda/shared/schemas.py`
Moved from `lambda/supervisor/schemas.py`:
- `AgentError` — custom exception with category
- `TokenUsage` — LLM token accounting model
- `ToolProvider` (Protocol) — abstract interface for tool calls
- `McpToolProvider` — production MCP client implementation
- `MockToolProvider` — test mock implementation

### `lambda/shared/agent_common.py`
Moved from `lambda/supervisor/agent.py`:
- `classify_error()` — maps exceptions → `AgentError`
- `check_deadline()` — checks remaining time against buffer
- `validate_tool_args()` — validates tool arguments via schemas
- `validate_tool_response()` — validates tool response JSON
- `_serialize_messages()` — converts LangChain messages into audit trail
- `PERMANENT_CATEGORIES = frozenset({"bedrock_auth", "unknown"})` — retry logic constant
- `DEADLINE_BUFFER = 90`
- Nudge logic (see Fix 8 below)

**Modified files after extraction:**
- `lambda/supervisor/schemas.py` — remove moved classes, re-export from `shared.schemas`
- `lambda/supervisor/agent.py` — import from `shared.agent_common` instead of local definitions

---

## 2. Resolver Schemas (`lambda/resolver/schemas.py`)

### Typed `parameters` (Fix 2)

Replace `parameters: dict` with typed unions so the LLM's output is validated at the schema level:

```python
class RestoreIAMPolicyParams(BaseModel):
    role_name: str
    policy_name: str
    policy_document: dict  # {Version, Statement[]}

class UpdateConcurrencyParams(BaseModel):
    function_name: str
    reserved_concurrent_executions: int | None  # None = delete reservation

class RemediationAction(BaseModel):
    fault_type: str            # "permission_loss" | "throttling" | "network_block"
    action_type: str           # "restore_iam_policy" | "update_concurrency" | "remove_sg_rule"
    target_resource: str       # ARN or identifier
    parameters: RestoreIAMPolicyParams | UpdateConcurrencyParams
    risk_level: str            # "low" | "medium" | "high"
    requires_approval: bool
    reasoning: str             # Why this action fixes the fault
    execution_order: int       # Auto-set by RemediationProposal validator
```

`@model_validator` on `RemediationAction` ensures `parameters` type matches `action_type`:
- `restore_iam_policy` → `RestoreIAMPolicyParams`
- `update_concurrency` → `UpdateConcurrencyParams`

### Multi-fault ordering (Fix 6)

Hardcoded priority map — LLM doesn't decide execution order:

```python
FAULT_EXECUTION_ORDER = {"permission_loss": 1, "throttling": 2, "network_block": 3}
```

`RemediationProposal` has a `@model_validator` that auto-sorts `actions` by `FAULT_EXECUTION_ORDER` and sets each action's `execution_order` field.

```python
class RemediationProposal(BaseModel):
    incident_id: str
    actions: list[RemediationAction]
    confidence: str            # "high" | "medium" | "low"
    chain_of_thought: str
    unresolvable_faults: list[str]
```

### Tool arg/response schemas

Same pattern as supervisor:
- `GetIAMRestoreInfoArgs(lambda_name: str)`
- `GetConcurrencyInfoArgs(lambda_name: str)`
- `IAMRestoreInfoResponse` — diff result + proposed policy doc
- `ConcurrencyInfoResponse` — current concurrency + proposed value

Imports `AgentError`, `TokenUsage`, `ToolProvider`, `McpToolProvider`, `MockToolProvider` from `shared.schemas`.

---

## 3. Resolver MCP Server (`mcp/resolver/`)

Structure mirrors `mcp/supervisor/`. Two read-only lookup tools (SG skipped until SG chaos is implemented):

| Tool | Purpose | AWS Calls |
|------|---------|-----------|
| `get_iam_restore_info` | Compare current IAM state vs known-good policy, return diff + proposed policy doc | `iam.get_role_policy()`, compare against known-good statements |
| `get_concurrency_restore_info` | Check current reserved concurrency, propose removal | `lambda.get_function_configuration()` |

### IAM constants from chaos package (Fix 3)

`mcp/resolver/tools/iam_remediation.py` imports directly from the chaos package:

```python
from chaos.iam_chaos import S3_STATEMENT, CLOUDWATCH_STATEMENT
```

Single source of truth — no drift possible. Requires `chaos/__init__.py` to make it a proper package.

### MCP auth (Fix 9)

Resolver MCP server on port 8081 uses identical `AuthMiddleware` as supervisor (port 8080):
- **Same API key** — both MCP servers on the same EC2 share one key
- Same SSM parameter: `/incident-response/mcp-api-key`
- Resolver Lambda's `get_mcp_api_key()` reads from the same SSM path as supervisor
- No new SSM parameters or IAM changes needed

### Files
- `mcp/resolver/server.py` — FastMCP + AuthMiddleware + `/health` (same pattern as supervisor)
- `mcp/resolver/tools/__init__.py`
- `mcp/resolver/tools/iam_remediation.py` — imports `S3_STATEMENT`, `CLOUDWATCH_STATEMENT` from `chaos.iam_chaos`
- `mcp/resolver/tools/concurrency_remediation.py`
- `mcp/resolver/Dockerfile`
- `mcp/resolver/requirements.txt`

---

## 4. Resolver LangGraph Agent (`lambda/resolver/agent.py`)

Mirrors supervisor's `agent.py` structure. Simpler — fewer iterations needed.

Imports `classify_error`, `check_deadline`, `validate_tool_args`, `validate_tool_response`, `_serialize_messages`, `PERMANENT_CATEGORIES`, `DEADLINE_BUFFER` from `shared.agent_common`.

Imports `AgentError`, `TokenUsage`, `ToolProvider`, `McpToolProvider` from `shared.schemas`.

### State (Fix 8 — `_nudge_count` replaces `_nudged`)

```python
class ResolverState(TypedDict):
    messages: Annotated[list, add_messages]
    diagnosis: dict
    incident_id: str
    proposal: RemediationProposal | None
    deadline: float
    token_usage: Annotated[list[TokenUsage], operator.add]
    _nudge_count: int  # replaces _nudged: bool
```

Nudge logic (in `shared/agent_common.py`, used by both agents):
- `nudge()` increments `_nudge_count`
- Router checks `_nudge_count < max_nudges` (default 1); if exceeded → END with no proposal/diagnosis
- Both supervisor and resolver reuse this logic

### Graph
`agent_reason → [route] → execute_tools / extract_proposal / nudge / END`

- Same nodes as supervisor but with `submit_proposal` instead of `submit_diagnosis`
- `RECURSION_LIMIT = 8` (simpler task)
- System prompt maps `fault_type` → tool, instructs LLM to build concrete proposals

### Tools bound to LLM
1. `get_iam_restore_info(lambda_name)`
2. `get_concurrency_restore_info(lambda_name)`
3. `submit_proposal(...)` — terminal tool, args = `RemediationProposal`

---

## 5. Resolver Lambda Handler (`lambda/resolver/handler.py`)

### Deadline propagation (Fix 5)

Handler computes deadline from `remaining_time_ms` passed by supervisor:

```python
def handler(event, context):
    # event = {"incident_id": "...", "diagnosis": {...}, "remaining_time_ms": 50000}
    deadline = time.time() + event.get("remaining_time_ms", 60000) / 1000
    # ... run agent with this deadline ...
    # Returns {"statusCode": 200, "proposal": {...}, "reasoning_chain": [...], "token_usage": [...]}
    # Or {"statusCode": 200, "proposal": None, "error": "..."}
```

No DynamoDB writes — supervisor owns state.

---

## 6. Supervisor Integration (`lambda/supervisor/orchestrator.py`)

### Resolver invocation with timeout/failure handling (Fix 4)

After diagnosis, invoke resolver with try/except:

```python
# Compute remaining time for resolver (Fix 5)
remaining_ms = context.get_remaining_time_in_millis() - 10000  # 10s buffer for cleanup
resolver_payload = {
    "incident_id": incident_id,
    "diagnosis": diagnosis.model_dump(),
    "remaining_time_ms": remaining_ms,
}

try:
    resp = lambda_client.invoke(
        FunctionName="resolver-agent",
        InvocationType="RequestResponse",
        Payload=json.dumps(resolver_payload),
    )
    if resp.get("FunctionError"):
        → transition to PROPOSAL_FAILED, log error
    proposal = parse response
    if proposal is None:
        → transition to PROPOSAL_FAILED
    else:
        → _store_proposal(incident_id, proposal, reasoning_chain)
        → transition to PROPOSAL_READY
except ReadTimeoutError:
    → transition to PROPOSAL_FAILED
except Exception:
    → transition to PROPOSAL_FAILED
```

New helper: `_store_proposal(incident_id, proposal, reasoning_chain)` — appends proposal to `incident-context` DynamoDB record.

New states: `DIAGNOSED → PROPOSAL_READY` or `DIAGNOSED → PROPOSAL_FAILED`

---

## 7. File List

### New files
- `lambda/shared/__init__.py`
- `lambda/shared/schemas.py`
- `lambda/shared/agent_common.py`
- `chaos/__init__.py`
- `lambda/resolver/__init__.py`
- `lambda/resolver/schemas.py`
- `lambda/resolver/agent.py`
- `lambda/resolver/handler.py`
- `lambda/resolver/tests/__init__.py`
- `lambda/resolver/tests/conftest.py`
- `lambda/resolver/tests/test_schemas.py`
- `lambda/resolver/tests/test_agent.py`
- `lambda/resolver/tests/test_handler.py`
- `mcp/resolver/__init__.py`
- `mcp/resolver/server.py`
- `mcp/resolver/Dockerfile`
- `mcp/resolver/requirements.txt`
- `mcp/resolver/tools/__init__.py`
- `mcp/resolver/tools/iam_remediation.py`
- `mcp/resolver/tools/concurrency_remediation.py`
- `mcp/resolver/tests/__init__.py`
- `mcp/resolver/tests/conftest.py`
- `mcp/resolver/tests/test_tools.py`
- `mcp/resolver/tests/test_server.py`

### Modified files
- `lambda/supervisor/schemas.py` — remove shared classes, re-export from `shared.schemas`
- `lambda/supervisor/agent.py` — import from `shared.agent_common`, use `_nudge_count`
- `lambda/supervisor/orchestrator.py` — invoke resolver after diagnosis with timeout handling

---

## 8. Verification

1. `python3 -m pytest lambda/supervisor/tests/ -v` — existing tests still pass after shared refactor
2. `python3 -c "from lambda.shared.schemas import AgentError, TokenUsage"` — shared imports work
3. `python3 -c "from chaos.iam_chaos import S3_STATEMENT"` — chaos package import works
4. `python3 -m pytest lambda/resolver/tests/ mcp/resolver/tests/ -v` — new tests pass
5. Supervisor integration test: mock `lambda_client.invoke` in existing orchestrator tests
6. Manual: run chaos → trigger incident → verify supervisor calls resolver → check DynamoDB for proposal

---

## Decisions

- **Lambda name**: `resolver-agent`
- **MCP deployment**: Same EC2 as supervisor, port 8081
- **Shared code**: `lambda/shared/` package (not duplicated)
- **SG tool**: Skipped until SG chaos is implemented
- **MCP auth**: Same API key + SSM parameter as supervisor
- **Partial remediation**: Deferred to Critic agent

## Design Fixes Summary

| # | Fix | Approach |
|---|-----|----------|
| 1 | Shared package | `lambda/shared/` with `schemas.py` + `agent_common.py` |
| 2 | Typed parameters | `RestoreIAMPolicyParams \| UpdateConcurrencyParams` union + validator |
| 3 | IAM constants | Import `S3_STATEMENT`/`CLOUDWATCH_STATEMENT` from `chaos.iam_chaos` |
| 4 | Resolver timeout handling | try/except in supervisor with `PROPOSAL_FAILED` state |
| 5 | Deadline propagation | Supervisor passes `remaining_time_ms` in resolver payload |
| 6 | Multi-fault ordering | Hardcoded `FAULT_EXECUTION_ORDER` map, auto-sorted by validator |
| 7 | Partial remediation | No code — deferred to Critic agent (comment only) |
| 8 | Nudge count | `_nudge_count: int` replaces `_nudged: bool` in shared logic |
| 9 | MCP auth | Same `AuthMiddleware`, same API key, same SSM path |

## Unresolved Questions

None.
