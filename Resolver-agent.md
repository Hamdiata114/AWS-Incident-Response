# Resolver Agent — Implementation Plan

## Context

The supervisor agent diagnoses faults and produces a `Diagnosis`. We need a resolver agent that takes that diagnosis and produces **concrete, executable remediation proposals** with exact AWS API parameters (e.g. `put_role_policy` kwargs). The resolver only proposes — no execution. The future Critic agent will approve proposals before execution.

## Decisions

- **Shared code**: Extract to `lambda/shared/` (two-phase: schemas first, then utils)
- **Invocation**: Async via dedicated SNS topic `resolver-trigger`
- **Diagnosis**: Payload only (supervisor passes full diagnosis)
- **DynamoDB writes**: Resolver writes to both `incident-state` (status) and `incident-audit` (reasoning chain)
- **Retry**: Extend watchdog to retry `PROPOSAL_FAILED` incidents
- **Network block**: Deferred
- **Resolver IAM role**: Create `resolver-agent-role` (Bedrock + SSM + DynamoDB + SNS)
- **Resolver Lambda timeout**: 120s
- **`PROPOSAL_FAILED` behavior**: Watchdog retries via SNS re-publish (max 2 retries)

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

### Packaging
- Add a build script that copies `config/` and `lambda/shared/` into the Lambda zip flat structure
- `conftest.py` files add `sys.path` entries for local dev (existing pattern)
- MCP Dockerfile copies `config/` (see Step 4)

### Verify
```bash
python3 -c "from config.baseline import S3_STATEMENT, CLOUDWATCH_STATEMENT; print('OK')"
python3 -m pytest chaos/tests/ -v
```

---

## Step 2 — Shared Agent Utilities

**Goal:** Extract reusable code from supervisor into `lambda/shared/` so the resolver can import it.

### Two-phase extraction
1. **Phase A — schemas**: Move classes, verify supervisor tests pass
2. **Phase B — agent_utils**: Move functions, verify supervisor tests pass

### Create
- `lambda/shared/__init__.py`
- `lambda/shared/schemas.py` — move from `lambda/supervisor/schemas.py`:
  - `AgentError`, `TokenUsage`, `ToolProvider` (Protocol), `McpToolProvider`, `MockToolProvider`
- `lambda/shared/agent_utils.py` — move from `lambda/supervisor/agent.py`:
  - `classify_error()`, `check_deadline()`, `validate_tool_args()`, `validate_tool_response()`, `_serialize_messages()` (rename to `serialize_messages`)
  - `PERMANENT_CATEGORIES`, `DEADLINE_BUFFER`

### Signature rules
- **Don't change function signatures**. `validate_tool_args(name, args)` keeps using a module-level schemas dict. Each agent sets its own.
- Re-exports in `lambda/supervisor/schemas.py` are fine.

### Modify
- `lambda/supervisor/schemas.py` — remove moved classes, re-export from `shared.schemas` for backwards compat
- `lambda/supervisor/agent.py` — import from `shared.agent_utils` and `shared.schemas`
- `lambda/supervisor/tests/conftest.py` — add `lambda/shared` to `sys.path`

### Tests — `lambda/shared/tests/` (new)

| File | Cases |
|------|-------|
| `test_classify_error.py` | timeout, connection, auth, transient, unknown |
| `test_check_deadline.py` | expired, within buffer, plenty of time |
| `test_validate_tool_args.py` | valid, missing required, extra fields |
| `test_validate_tool_response.py` | valid, malformed JSON, schema mismatch |
| `test_providers.py` | MockToolProvider returns expected responses |

### Verify
```bash
python3 -m pytest lambda/shared/tests/ -v    # new shared tests
python3 -m pytest lambda/supervisor/tests/ -v # all existing tests still pass
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

### Tests — `mcp/resolver/tests/test_tools.py` (expand)
- Drift scenarios: missing S3 stmt, missing CW stmt, both missing, policy detached, no drift
- Concurrency: 0 (throttled), 1 (throttled), None (healthy)

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

### Tests — `lambda/resolver/tests/test_agent.py` (expand)
- Use `MockToolProvider`
- Mock-LLM per fault: `permission_loss` → `get_baseline_iam` → `put_role_policy` proposal
- Mock-LLM per fault: `throttling` → `get_current_concurrency` → `delete_function_concurrency` proposal
- Multi-fault: both tools called, multiple actions
- Error paths: MCP unreachable, invalid schema, deadline exceeded

### Verify
```bash
python3 -m pytest lambda/resolver/tests/test_agent.py -v
```

---

## Step 6 — Resolver Lambda Handler

**Goal:** Lambda entry point triggered by SNS `resolver-trigger` topic. Writes to DynamoDB.

### Create `lambda/resolver/handler.py`

```python
def handler(event, context):
    # event = SNS event wrapping {"incident_id": "...", "diagnosis": {...}}
    # Parses SNS event
    # Runs resolver agent
    # Writes to incident-state (status) and incident-audit (reasoning + tokens)
    # Status outcomes: PROPOSED | PROPOSAL_FAILED
```

### DynamoDB writes (resolver owns these)
- `incident-state`: transition `RESOLVING → PROPOSED` or `RESOLVING → PROPOSAL_FAILED`
- `incident-audit`: write reasoning chain + token usage

### Create
- `lambda/resolver/requirements.txt` (same deps as supervisor)
- `lambda/resolver/tests/` — `__init__.py`, `conftest.py`, `test_handler.py`

### Tests — `lambda/resolver/tests/test_handler.py` (expand)
- Happy path: SNS event → DynamoDB gets `PROPOSED` status + audit entry
- MCP failure → `PROPOSAL_FAILED`
- Invalid diagnosis → `PROPOSAL_FAILED`
- None proposal → `PROPOSAL_FAILED`

### Verify
```bash
python3 -m pytest lambda/resolver/tests/ -v
```

---

## Step 7 — Supervisor Integration (Async SNS)

**Goal:** After DIAGNOSED, supervisor publishes to SNS instead of sync Lambda invoke.

### Modify `lambda/supervisor/orchestrator.py`

After `transition_state(incident_id, "INVESTIGATING", "DIAGNOSED")` (line 379):

```python
transition_state(incident_id, "DIAGNOSED", "RESOLVING")
sns.publish(
    TopicArn="arn:aws:sns:ca-central-1:534321188934:resolver-trigger",
    Message=json.dumps({"incident_id": incident_id, "diagnosis": diagnosis.model_dump()})
)
# Supervisor's job ends at RESOLVING — resolver Lambda picks up async
```

**New states:** `DIAGNOSED → RESOLVING → PROPOSED | PROPOSAL_FAILED`

**New infra:**
- SNS topic `resolver-trigger`
- Lambda subscription (resolver-agent subscribes to topic)
- Resolver IAM for DynamoDB + SNS

### Tests — `lambda/supervisor/tests/test_orchestrator.py` (add)
- `DIAGNOSED → RESOLVING` transition + SNS publish verified
- SNS publish failure → stays at `DIAGNOSED`

### Verify
```bash
python3 -m pytest lambda/supervisor/tests/test_orchestrator.py -v
```

---

## Step 8 — Watchdog Retry

**Goal:** Extend watchdog to retry `PROPOSAL_FAILED` incidents.

### Modify `lambda/watchdog/handler.py`
- Scan for `PROPOSAL_FAILED` incidents older than N minutes
- Re-publish to `resolver-trigger` SNS topic (max 2 retries, tracked in incident-state `retry_count`)

### Tests — `lambda/watchdog/tests/test_retry.py` (new)
- Retry logic tests

### Verify
```bash
python3 -m pytest lambda/watchdog/tests/ -v
```

---

## Step 9 — Deploy

1. Create SNS topic `resolver-trigger`
2. Update SG `sg-096fd53730c49713b` — allow inbound 8081
3. Build + run resolver MCP container on EC2 (port 8081)
4. Package + deploy `resolver-agent` Lambda (build deps on Linux via Docker)
5. Set Lambda env vars: `MCP_SERVER_URL`, `MCP_API_KEY`
6. Subscribe resolver Lambda to `resolver-trigger` SNS topic
7. Grant resolver Lambda permissions: Bedrock, SSM, DynamoDB, SNS

### E2E smoke test
- Script: inject fault → wait for `PROPOSED` in DynamoDB → validate proposal actions match fault type

### Verify
```bash
curl http://3.99.16.1:8081/health
# Then: run chaos → trigger incident → check DynamoDB for proposal
```

---

## Files Modified (vs original plan)

| Original plan file | What changes |
|---|---|
| `lambda/supervisor/orchestrator.py` | SNS publish instead of sync Lambda invoke |
| `lambda/watchdog/handler.py` | Add `PROPOSAL_FAILED` retry scan |
| `lambda/resolver/handler.py` | Parse SNS event (not direct invoke), write to DynamoDB |

## New Files (vs original plan)

| File | Purpose |
|---|---|
| `lambda/shared/tests/*` | 5 test files for extracted utilities |
| `lambda/watchdog/tests/test_retry.py` | Watchdog retry tests |

---

## Unresolved Questions

None — all decisions resolved.
