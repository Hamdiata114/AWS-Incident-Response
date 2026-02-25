# Resolver Agent — Step-by-Step Implementation

## Overview

The supervisor produces a `Diagnosis`. The resolver takes it and produces **concrete, executable remediation proposals**. It is a separate Lambda invoked synchronously by the supervisor, with its own MCP server for remediation-lookup tools.

**Out of scope:** Critic approval, execution-time partial failure handling.

---

## Step 1 — Shared Package

**Goal:** Eliminate code duplication between supervisor and resolver.

### Create

- `lambda/shared/__init__.py` — empty
- `lambda/shared/schemas.py` — move from `lambda/supervisor/schemas.py`:
  - `AgentError`, `TokenUsage`, `ToolProvider`, `McpToolProvider`, `MockToolProvider`
- `lambda/shared/agent_common.py` — move from `lambda/supervisor/agent.py`:
  - `classify_error()`, `check_deadline()`, `validate_tool_args()`, `validate_tool_response()`, `_serialize_messages()`
  - `PERMANENT_CATEGORIES`, `DEADLINE_BUFFER = 90`
  - Nudge logic: `_nudge_count: int` (replaces `_nudged: bool`); router checks `_nudge_count < max_nudges`

### Modify

- `lambda/supervisor/schemas.py` — remove moved classes, re-export from `shared.schemas`
- `lambda/supervisor/agent.py` — import from `shared.agent_common`; switch to `_nudge_count`

### Verify

```
python3 -c "from lambda.shared.schemas import AgentError, TokenUsage"
python3 -m pytest lambda/supervisor/tests/ -v   # existing tests must still pass
```

---

## Step 2 — Chaos Package Init

**Goal:** Allow `mcp/resolver` to import IAM constants directly from the chaos package.

### Create

- `chaos/__init__.py` — empty (makes `chaos` a proper package)

### Verify

```
python3 -c "from chaos.iam_chaos import S3_STATEMENT, CLOUDWATCH_STATEMENT"
```

---

## Step 3 — Resolver Schemas

**Goal:** Define typed models for the resolver's input/output.

### Create `lambda/resolver/schemas.py`

```python
class RestoreIAMPolicyParams(BaseModel):
    role_name: str
    policy_name: str
    policy_document: dict  # {Version, Statement[]}

class UpdateConcurrencyParams(BaseModel):
    function_name: str
    reserved_concurrent_executions: int | None  # None = delete reservation

class RemediationAction(BaseModel):
    fault_type: str        # "permission_loss" | "throttling" | "network_block"
    action_type: str       # "restore_iam_policy" | "update_concurrency"
    target_resource: str
    parameters: RestoreIAMPolicyParams | UpdateConcurrencyParams
    risk_level: str        # "low" | "medium" | "high"
    requires_approval: bool
    reasoning: str
    execution_order: int   # auto-set by RemediationProposal validator

class RemediationProposal(BaseModel):
    incident_id: str
    actions: list[RemediationAction]
    confidence: str        # "high" | "medium" | "low"
    chain_of_thought: str
    unresolvable_faults: list[str]
```

**Validators:**
- `RemediationAction`: `@model_validator` ensures `parameters` type matches `action_type`
- `RemediationProposal`: auto-sorts `actions` by hardcoded priority map and sets `execution_order`

```python
FAULT_EXECUTION_ORDER = {"permission_loss": 1, "throttling": 2, "network_block": 3}
```

**Tool arg/response schemas** (same pattern as supervisor):
- `GetIAMRestoreInfoArgs(lambda_name: str)`
- `GetConcurrencyInfoArgs(lambda_name: str)`
- `IAMRestoreInfoResponse` — diff result + proposed policy doc
- `ConcurrencyInfoResponse` — current concurrency + proposed value

Imports `AgentError`, `TokenUsage`, `ToolProvider`, `McpToolProvider`, `MockToolProvider` from `shared.schemas`.

### Verify

```
python3 -m pytest lambda/resolver/tests/test_schemas.py -v
```

---

## Step 4 — Resolver MCP Server

**Goal:** Provide two read-only lookup tools on port 8081 (same EC2 as supervisor).

### Create

```
mcp/resolver/__init__.py
mcp/resolver/server.py
mcp/resolver/Dockerfile
mcp/resolver/requirements.txt
mcp/resolver/tools/__init__.py
mcp/resolver/tools/iam_remediation.py
mcp/resolver/tools/concurrency_remediation.py
```

### Tools

| Tool | Purpose | AWS calls |
|------|---------|-----------|
| `get_iam_restore_info(lambda_name)` | Diff current IAM vs known-good, return proposed policy doc | `iam.get_role_policy()` |
| `get_concurrency_restore_info(lambda_name)` | Check reserved concurrency, propose removal | `lambda.get_function_configuration()` |

SG tool skipped until SG chaos is implemented.

**IAM constants** — import directly from chaos package (single source of truth):
```python
from chaos.iam_chaos import S3_STATEMENT, CLOUDWATCH_STATEMENT
```

**Auth** — same `AuthMiddleware` as supervisor (port 8080), same API key, same SSM parameter `/incident-response/mcp-api-key`. No new SSM params or IAM changes needed.

**`server.py`** — FastMCP + `AuthMiddleware` + `/health` endpoint (same pattern as supervisor, port 8081).

### Verify

```
python3 -m pytest mcp/resolver/tests/ -v
# Then on EC2:
docker build -t resolver-mcp ./mcp/resolver && docker run -p 8081:8081 resolver-mcp
curl http://localhost:8081/health
```

---

## Step 5 — Resolver LangGraph Agent

**Goal:** LangGraph agent that calls MCP tools and produces a `RemediationProposal`.

### Create `lambda/resolver/agent.py`

**State:**
```python
class ResolverState(TypedDict):
    messages: Annotated[list, add_messages]
    diagnosis: dict
    incident_id: str
    proposal: RemediationProposal | None
    deadline: float
    token_usage: Annotated[list[TokenUsage], operator.add]
    _nudge_count: int
```

**Graph:** `agent_reason → [route] → execute_tools / extract_proposal / nudge / END`

- `RECURSION_LIMIT = 8`
- System prompt maps `fault_type` → tool, instructs LLM to build concrete proposals

**Tools bound to LLM:**
1. `get_iam_restore_info(lambda_name)`
2. `get_concurrency_restore_info(lambda_name)`
3. `submit_proposal(...)` — terminal tool, args = `RemediationProposal`

Imports all shared utilities from `shared.agent_common` and `shared.schemas`.

### Verify

```
python3 -m pytest lambda/resolver/tests/test_agent.py -v
```

---

## Step 6 — Resolver Lambda Handler

**Goal:** Lambda entry point with deadline propagation.

### Create `lambda/resolver/handler.py`

```python
def handler(event, context):
    # event = {"incident_id": "...", "diagnosis": {...}, "remaining_time_ms": 50000}
    deadline = time.time() + event.get("remaining_time_ms", 60000) / 1000
    # run agent
    # Returns:
    #   success → {"statusCode": 200, "proposal": {...}, "reasoning_chain": [...], "token_usage": [...]}
    #   failure → {"statusCode": 200, "proposal": None, "error": "..."}
```

No DynamoDB writes — supervisor owns state.

### Verify

```
python3 -m pytest lambda/resolver/tests/test_handler.py -v
```

---

## Step 7 — Supervisor Integration

**Goal:** Invoke resolver after diagnosis; handle timeouts and failures.

### Modify `lambda/supervisor/orchestrator.py`

```python
remaining_ms = context.get_remaining_time_in_millis() - 10000  # 10s cleanup buffer
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
        → transition to PROPOSAL_FAILED
    proposal = parse response
    if proposal is None:
        → transition to PROPOSAL_FAILED
    else:
        _store_proposal(incident_id, proposal, reasoning_chain)
        → transition to PROPOSAL_READY
except (ReadTimeoutError, Exception):
    → transition to PROPOSAL_FAILED
```

**New helper:** `_store_proposal(incident_id, proposal, reasoning_chain)` — appends proposal to `incident-context` DynamoDB record.

**New states:** `DIAGNOSED → PROPOSAL_READY` or `DIAGNOSED → PROPOSAL_FAILED`

### Verify

```
# mock lambda_client.invoke in orchestrator tests
python3 -m pytest lambda/supervisor/tests/ -v
```

---

## Step 8 — Deploy & End-to-End Test

1. Deploy resolver MCP container on EC2 (port 8081)
2. Deploy `resolver-agent` Lambda
3. Run chaos → trigger incident → verify supervisor calls resolver → check DynamoDB for proposal

---

## Implementation Order

```
Step 1 (shared)
  └── Step 2 (chaos init)
        ├── Step 3 (resolver schemas)
        │     ├── Step 4 (MCP server)
        │     └── Step 5 (agent)
        │           └── Step 6 (handler)
        │                 └── Step 7 (supervisor integration)
        │                       └── Step 8 (deploy)
```

---

## Decisions

| Decision | Value |
|----------|-------|
| Lambda name | `resolver-agent` |
| MCP port | 8081, same EC2 as supervisor |
| Shared code | `lambda/shared/` |
| SG tool | Skipped |
| MCP auth | Same key + SSM param as supervisor |
| Partial remediation | Deferred to Critic agent |
