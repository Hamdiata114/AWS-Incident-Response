# Resolver Agent — Implementation Plan

## Context

The supervisor agent diagnoses faults and produces a `Diagnosis`. We need a resolver agent that takes that diagnosis and produces **concrete, executable remediation proposals** with exact AWS API parameters (e.g. `put_role_policy` kwargs). The resolver only proposes — no execution. The future Critic agent will approve proposals before execution.

---

## Step 1 — Shared Config Baseline

**Goal:** Single source of truth for known-good IAM constants. Both chaos script and resolver MCP import from here.

### Create
- `config/__init__.py` — empty
- `config/baseline.py` — extract from `chaos/iam_chaos.py`:
  - `ROLE_NAME`, `POLICY_NAME`, `ACCOUNT_ID`, `REGION`
  - `S3_STATEMENT`, `CLOUDWATCH_STATEMENT`
  - `FULL_POLICY_DOCUMENT` (new convenience dict with both statements)

### Modify
- `chaos/iam_chaos.py` — replace hardcoded constants with `from config.baseline import ...`
- `chaos/tests/test_iam_chaos.py` — update if tests reference the old constants location

### Verify
```bash
python3 -c "from config.baseline import S3_STATEMENT, CLOUDWATCH_STATEMENT; print('OK')"
python3 -m pytest chaos/tests/ -v
```

---

## Step 2 — Shared Agent Utilities

**Goal:** Extract reusable code from supervisor into `lambda/shared/` so the resolver can import it.

### Create
- `lambda/shared/__init__.py`
- `lambda/shared/schemas.py` — move from `lambda/supervisor/schemas.py`:
  - `AgentError`, `TokenUsage`, `ToolProvider` (Protocol), `McpToolProvider`, `MockToolProvider`
- `lambda/shared/agent_utils.py` — move from `lambda/supervisor/agent.py`:
  - `classify_error()`, `check_deadline()`, `validate_tool_args()`, `validate_tool_response()`, `_serialize_messages()` (rename to `serialize_messages`)
  - `PERMANENT_CATEGORIES`, `DEADLINE_BUFFER`
  - **Fix:** `check_deadline(state, buffer=90)` — accept any dict with `"deadline"` key, not typed to `AgentState`
  - **Fix:** `validate_tool_args(name, args, arg_schemas)` / `validate_tool_response(name, json, response_schemas)` — take schema dicts as parameters instead of importing globals

### Modify
- `lambda/supervisor/schemas.py` — remove moved classes, re-export from `shared.schemas` for backwards compat
- `lambda/supervisor/agent.py` — import from `shared.agent_utils` and `shared.schemas`; pass `TOOL_ARG_SCHEMAS`/`TOOL_RESPONSE_SCHEMAS` to validation calls
- `lambda/supervisor/tests/conftest.py` — add `lambda/shared` to `sys.path`

### Verify
```bash
python3 -m pytest lambda/supervisor/tests/ -v   # all existing tests pass
```

---

## Step 3 — Resolver Schemas

**Goal:** Typed models for the resolver's proposal output and MCP tool schemas.

### Create
- `lambda/resolver/__init__.py`
- `lambda/resolver/schemas.py`:

```python
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

# Tool arg/response schemas (same pattern as supervisor)
class GetBaselineIAMArgs(BaseModel): ...
class GetCurrentConcurrencyArgs(BaseModel): ...
class BaselineIAMResponse(BaseModel): ...    # role_name, policy_name, expected_policy, current_policy, drift
class ConcurrencyResponse(BaseModel): ...    # lambda_name, reserved_concurrency, is_throttled

TOOL_ARG_SCHEMAS = { "get_baseline_iam": ..., "get_current_concurrency": ... }
TOOL_RESPONSE_SCHEMAS = { "get_baseline_iam": ..., "get_current_concurrency": ... }
```

### Verify
```bash
python3 -c "from resolver.schemas import RemediationProposal; print('OK')"
```

---

## Step 4 — Resolver MCP Server

**Goal:** MCP server on port 8081 with two remediation-lookup tools. Same EC2, same auth pattern.

### Create
```
mcp/resolver/
  server.py           # FastMCP + AuthMiddleware + /health (port 8081)
  requirements.txt
  Dockerfile
  tools/
    __init__.py
    iam_baseline.py    # get_baseline_iam: diffs current IAM vs config.baseline
    concurrency.py     # get_current_concurrency: checks reserved concurrency
  tests/
    __init__.py
    conftest.py
    test_tools.py
```

**Tools:**

| Tool | What it does | AWS call |
|------|-------------|----------|
| `tool_get_baseline_iam(role_name)` | Compare current inline policy vs `FULL_POLICY_DOCUMENT` from `config.baseline`, return drift | `iam.get_role_policy()` |
| `tool_get_current_concurrency(lambda_name)` | Get reserved concurrency, flag if throttled (0 or 1) | `lambda.get_function_configuration()` |

**Docker:** Needs `config/baseline.py` in the image. Build with repo root as context:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY config/ /app/config/
COPY mcp/resolver/ /app/
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8081
CMD ["python", "server.py"]
```

**Auth:** Same API key, same SSM parameter as supervisor. No new infra.

### Verify
```bash
python3 -m pytest mcp/resolver/tests/ -v
```

---

## Step 5 — Resolver LangGraph Agent

**Goal:** ReAct agent that takes a Diagnosis and produces a `RemediationProposal`.

### Create `lambda/resolver/agent.py`

**State:**
```python
class ResolverState(TypedDict):
    messages: Annotated[list, add_messages]
    incident_id: str
    diagnosis: dict
    proposal: RemediationProposal | None
    deadline: float
    token_usage: Annotated[list[TokenUsage], operator.add]
    _nudged: bool
```

**Graph:** `agent_reason → route → execute_tools / extract_proposal / nudge / END`
- `RECURSION_LIMIT = 8` (simpler task than diagnosis)
- System prompt maps fault_type → tool, instructs LLM to output exact boto3 parameters

**Tools bound to LLM:**
1. `get_baseline_iam(role_name)` — for permission_loss faults
2. `get_current_concurrency(lambda_name)` — for throttling faults
3. `submit_proposal(...)` — terminal tool, captures `RemediationProposal`

**Imports** from `shared.agent_utils` and `shared.schemas`.

**`run_agent` signature:**
```python
async def run_agent(diagnosis: dict, incident_id: str, lambda_context) -> dict
# Returns {"proposal": RemediationProposal | None, "reasoning_chain": [...], "token_usage": [...]}
```

### Verify
```bash
python3 -m pytest lambda/resolver/tests/test_agent.py -v
```

---

## Step 6 — Resolver Lambda Handler

**Goal:** Lambda entry point invoked synchronously by supervisor.

### Create `lambda/resolver/handler.py`

```python
def handler(event, context):
    # event = {"incident_id": "...", "diagnosis": {...}}
    # Returns:
    #   {"status": "PROPOSED", "proposal": {...}, "reasoning_chain": [...], "token_usage": [...]}
    #   {"status": "FAILED", "error": "..."}
```

No DynamoDB writes — supervisor owns state. No SNS parsing — invoked directly.

### Create
- `lambda/resolver/requirements.txt` (same deps as supervisor)
- `lambda/resolver/tests/` — `__init__.py`, `conftest.py`, `test_handler.py`

### Verify
```bash
python3 -m pytest lambda/resolver/tests/ -v
```

---

## Step 7 — Supervisor Integration

**Goal:** After DIAGNOSED, supervisor invokes resolver Lambda synchronously.

### Modify `lambda/supervisor/orchestrator.py`

After `transition_state(incident_id, "INVESTIGATING", "DIAGNOSED")` (line 379), instead of returning immediately:

```python
transition_state(incident_id, "DIAGNOSED", "RESOLVING")
try:
    resp = lambda_client.invoke(FunctionName="resolver-agent", ...)
    if ok: transition_state("RESOLVING", "PROPOSED"), store proposal
    else:  transition_state("RESOLVING", "PROPOSAL_FAILED")
except Exception:
    transition_state("RESOLVING", "PROPOSAL_FAILED")
```

**New states:** `DIAGNOSED → RESOLVING → PROPOSED | PROPOSAL_FAILED`

**New helper:** `_store_proposal()` — appends proposal to `incident-context` DynamoDB record.

### Verify
```bash
python3 -m pytest lambda/supervisor/tests/test_orchestrator.py -v
```

---

## Step 8 — Deploy

1. Update SG `sg-096fd53730c49713b` — allow inbound 8081
2. Build + run resolver MCP container on EC2 (port 8081)
3. Package + deploy `resolver-agent` Lambda (build deps on Linux via Docker)
4. Set Lambda env vars: `MCP_SERVER_URL`, `MCP_API_KEY`
5. Grant supervisor Lambda permission to invoke `resolver-agent`

### Verify
```bash
curl http://3.99.16.1:8081/health
# Then: run chaos → trigger incident → check DynamoDB for proposal
```

---

## Unresolved Questions

1. **Resolver IAM role** — reuse `supervisor-agent-role` or create `resolver-agent-role`? Resolver Lambda only needs Bedrock + SSM (tools run on MCP/EC2).
2. **Resolver Lambda timeout** — supervisor is 300s. Resolver is simpler — 120s?
3. **`PROPOSAL_FAILED` behavior** — should supervisor retry resolver, or just leave incident in `PROPOSAL_FAILED` state?
