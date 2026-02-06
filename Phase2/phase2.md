# Phase 2: Supervisor Context Enrichment via MCP

## Goal

Build a supervisor agent that receives incident alerts, gathers diagnostic evidence using MCP tools, and makes an autonomous decision about what went wrong and how to fix it. By the end of this phase, the supervisor should be able to: receive an SNS alert, pull logs/IAM state/Lambda config, reason over the evidence, and output a diagnosis with a proposed remediation — without human intervention.

## Overview

When supervisor-agent receives an incident via SNS, it calls diagnostic tools on its MCP server to gather context (logs, IAM state, Lambda config), persists the result to DynamoDB, then acts. The tools live on an MCP server running as a container on EC2 — not bundled in the Lambda.

## Decisions

- **Separate IAM role** for supervisor Lambda (`supervisor-agent-role`)
- **Separate IAM role** for MCP server EC2 (`supervisor-mcp-role`)
- **Sequential** tool calls
- **DynamoDB** persistence for enriched context
- **MCP server** on EC2 exposes investigative tools
- **Token budget** configurable via env var; metrics logged to CloudWatch (not stored in DynamoDB)

## Architecture

```
SNS incident arrives
    → supervisor Lambda (orchestrator.py) parses alert
    → write RECEIVED to incident-state
    → transition to INVESTIGATING
    → calls MCP server tools over HTTP (SSE transport)
        → get_recent_logs(lambda_name)
        → get_iam_state(lambda_name)
        → get_lambda_config(lambda_name)
    → assembles enriched context from tool results
    → persists to DynamoDB (incident-context table)
    → transition to CONTEXT_GATHERED (or FAILED on error)
    → enriched context ready for LangGraph agents (future)
```

```
┌─────────────────────┐         HTTP/SSE         ┌──────────────────────┐
│  supervisor Lambda   │ ──────────────────────→  │  MCP Server (EC2)    │
│  (MCP client)        │                          │  (Docker container)  │
│                      │ ←────────────────────── │                      │
│  - parse SNS event   │      tool results        │  Tools:              │
│  - call MCP tools    │                          │  - get_recent_logs   │
│  - persist to Dynamo │                          │  - get_iam_state     │
└─────────────────────┘                          │  - get_lambda_config │
                                                  └──────────────────────┘
```

## File Structure

```
mcp/
    supervisor/
        Dockerfile                   # Container image
        requirements.txt             # mcp, boto3
        server.py                    # MCP server entry point + tool definitions
        tools/
            __init__.py
            cloudwatch_logs.py       # get_recent_logs tool
            iam_policy.py            # get_iam_state tool
            lambda_config.py         # get_lambda_config tool

lambda/supervisor/
    orchestrator.py                  # Modify: MCP client + DynamoDB persist
    requirements.txt                 # mcp client SDK
```

---

## Step 1: Create DynamoDB Tables

### Step 1a: `incident-context` table (evidence store)

**Table:** `incident-context` in ca-central-1

| Attribute | Type | Key |
|-----------|------|-----|
| `incident_id` | S | Partition key — `{lambda_name}#{timestamp}` |
| `error_type` | S | — |
| `enriched_context` | S | JSON-serialized context |
| `created_at` | S | ISO timestamp |
| `ttl` | N | Auto-expire after 7 days |

Enable TTL on the `ttl` attribute.

### Step 1b: `incident-state` table (lifecycle tracking)

**Table:** `incident-state` in ca-central-1

| Attribute | Type | Key |
|-----------|------|-----|
| `incident_id` | S | Partition key — `{lambda_name}#{timestamp}` |
| `status` | S | — |
| `owner_agent` | S | Always `"supervisor"` in Phase 2 |
| `created_at` | S | ISO timestamp |
| `updated_at` | S | ISO timestamp (stale detection) |
| `error_reason` | S | Only when `status=FAILED` |
| `ttl` | N | Auto-expire after 7 days |

Enable TTL on the `ttl` attribute.

**Phase 2 statuses:** `RECEIVED → INVESTIGATING → CONTEXT_GATHERED | FAILED`

**Future statuses (out of scope):** `DIAGNOSING`, `REMEDIATING`, `AWAITING_APPROVAL`, `RESOLVED`

## Step 2: Launch EC2 Instance

- Region: ca-central-1
- Instance type: t3.micro (sufficient for MCP server)
- AMI: Amazon Linux 2023 with Docker
- Security group: allow inbound on MCP server port (e.g. 8080) from Lambda's VPC/security group
- IAM instance profile: `supervisor-mcp-role` (see Step 3)

## Step 3: Create IAM Roles

### a) `supervisor-mcp-role` (EC2 instance profile)

The MCP server needs AWS access to call investigative APIs.

| Action | Scoped Resource |
|--------|----------------|
| `logs:FilterLogEvents`, `logs:DescribeLogStreams` | `arn:aws:logs:ca-central-1:534321188934:log-group:/aws/lambda/*` |
| `lambda:GetFunction`, `lambda:GetFunctionConfiguration` | `arn:aws:lambda:ca-central-1:534321188934:function:data-processor` |
| `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` | `arn:aws:iam::534321188934:role/lab-lambda-baisc-role` |

Trust policy: EC2 service.

### b) `supervisor-agent-role` (Lambda execution role)

| Policy | Purpose |
|--------|---------|
| `AWSLambdaBasicExecutionRole` | CloudWatch logging |
| Inline: `supervisor-dynamodb-access` | `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:UpdateItem` on `arn:aws:dynamodb:ca-central-1:534321188934:table/incident-context` and `arn:aws:dynamodb:ca-central-1:534321188934:table/incident-state` |

Trust policy: Lambda service.

**Note:** The Lambda no longer needs AWS investigative permissions — those live on the MCP server's EC2 role.

## Step 4: Update Supervisor Lambda Role

Update `supervisor-agent` Lambda to use `supervisor-agent-role` instead of `lab-lambda-baisc-role`.

## Step 5: Implement MCP Server

### a) `mcp/supervisor/server.py`

MCP server entry point using the `mcp` Python SDK. Registers three tools:

```python
from mcp.server import Server
from mcp.server.sse import SseServerTransport

app = Server("supervisor-tools")

@app.tool()
async def get_recent_logs(lambda_name: str, minutes: int = 10) -> dict:
    """Fetch recent CloudWatch logs from a Lambda function."""

@app.tool()
async def get_iam_state(lambda_name: str) -> dict:
    """Get current IAM policy state for a Lambda's execution role."""

@app.tool()
async def get_lambda_config(lambda_name: str) -> dict:
    """Get Lambda function configuration metadata."""
```

### b) `mcp/supervisor/tools/cloudwatch_logs.py` — `get_recent_logs()`

- Log group: `/aws/lambda/{lambda_name}` (derived)
- Uses `filter_log_events`, `startTime` = now - `minutes` param, `limit=30`
- Each message truncated to 500 chars
- Returns `{"log_group": ..., "events": [{"timestamp": ..., "message": ...}, ...]}`

### c) `mcp/supervisor/tools/iam_policy.py` — `get_iam_state()`

- Gets role name from `lambda:GetFunction` (not hardcoded)
- Lists attached managed policies (ARNs only)
- Fetches all inline policy documents (full JSON)
- Returns `{"role_name": ..., "inline_policies": {...}, "attached_policies": [...]}`

### d) `mcp/supervisor/tools/lambda_config.py` — `get_lambda_config()`

- Calls `get_function_configuration`
- Returns subset: FunctionName, Runtime, Handler, Role, MemorySize, Timeout, LastModified, State, ReservedConcurrentExecutions
- **Strips Environment.Variables** for security

### e) `mcp/supervisor/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["python", "server.py"]
```

### f) `mcp/supervisor/requirements.txt`

```
mcp
boto3
```

## Step 6: Deploy MCP Server to EC2

1. Copy `mcp/supervisor/` to EC2 instance
2. Build Docker image
3. Run container on port 8080
4. Verify health: `curl http://<ec2-ip>:8080/sse`

## Step 7: Update `orchestrator.py` (MCP Client)

The supervisor Lambda becomes an MCP client. Token budget is read from an environment variable so it can be changed without redeploying code.

```python
import os
from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_SERVER_URL = "http://<ec2-private-ip>:8080/sse"
TOKEN_BUDGET = int(os.environ.get("TOKEN_BUDGET", "3000"))

async def gather_context(incident: dict) -> dict:
    lambda_name = incident["lambda_name"]
    context = {"incident": incident, "tools": {}}
    raw_sizes = {}

    async with sse_client(MCP_SERVER_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            logs = await session.call_tool("get_recent_logs", {
                "lambda_name": lambda_name
            })
            context["tools"]["cloudwatch_logs"] = logs
            raw_sizes["cloudwatch_logs"] = estimate_tokens(logs)

            iam = await session.call_tool("get_iam_state", {
                "lambda_name": lambda_name
            })
            context["tools"]["iam_policy"] = iam
            raw_sizes["iam_policy"] = estimate_tokens(iam)

            config = await session.call_tool("get_lambda_config", {
                "lambda_name": lambda_name
            })
            context["tools"]["lambda_config"] = config
            raw_sizes["lambda_config"] = estimate_tokens(config)

    # Truncate to budget
    raw_total = sum(raw_sizes.values())
    truncated_context, truncation_details = truncate_to_budget(context, TOKEN_BUDGET)
    final_total = estimate_tokens(truncated_context)

    # Build metrics
    metrics = {
        "token_budget": TOKEN_BUDGET,
        "raw_tokens_total": raw_total,
        "raw_tokens_per_tool": raw_sizes,
        "final_tokens": final_total,
        "truncated": raw_total > TOKEN_BUDGET,
        "truncation_details": truncation_details,  # which tools were cut, by how much
    }

    return truncated_context, metrics
```

### State management functions

Two functions manage the `incident-state` table:

```python
def write_initial_state(incident_id: str):
    """Create initial state record. Idempotent — skips if record already exists (duplicate SNS delivery)."""
    now = datetime.now(timezone.utc).isoformat()
    dynamodb.put_item(
        TableName="incident-state",
        Item={
            "incident_id": {"S": incident_id},
            "status": {"S": "RECEIVED"},
            "owner_agent": {"S": "supervisor"},
            "created_at": {"S": now},
            "updated_at": {"S": now},
            "ttl": {"N": str(int(time.time()) + 7 * 86400)},
        },
        ConditionExpression="attribute_not_exists(incident_id)",
    )


def transition_state(incident_id: str, from_status: str, to_status: str, error_reason: str = None):
    """Transition incident status with conditional check. Raises if current status != from_status."""
    now = datetime.now(timezone.utc).isoformat()
    update_expr = "SET #s = :to_status, updated_at = :now"
    expr_values = {":from_status": {"S": from_status}, ":to_status": {"S": to_status}, ":now": {"S": now}}
    expr_names = {"#s": "status"}

    if error_reason:
        update_expr += ", error_reason = :err"
        expr_values[":err"] = {"S": error_reason}

    dynamodb.update_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression=update_expr,
        ConditionExpression="#s = :from_status",
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )
```

### Orchestrator integration

The `lambda_handler` writes state before MCP calls, transitions after, and catches exceptions to mark `FAILED`:

```python
def lambda_handler(event, _context):
    incident = parse_sns_event(event)
    incident_id = f"{incident['lambda_name']}#{incident['timestamp']}"

    # 1. Record receipt (idempotent)
    write_initial_state(incident_id)
    transition_state(incident_id, "RECEIVED", "INVESTIGATING")

    try:
        # 2. Gather context via MCP
        context, metrics = await gather_context(incident)

        # 3. Log metrics
        logger.info(json.dumps({
            "event": "token_metrics",
            "incident_id": incident_id,
            "agent": "supervisor",
            "metrics": metrics
        }))

        # 4. Persist context
        dynamodb.put_item(TableName="incident-context", Item={
            "incident_id": {"S": incident_id},
            "error_type": {"S": incident["error_type"]},
            "enriched_context": {"S": json.dumps(context, default=str)},
            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
            "ttl": {"N": str(int(time.time()) + 7 * 86400)}
        })

        # 5. Mark success
        transition_state(incident_id, "INVESTIGATING", "CONTEXT_GATHERED")

    except Exception as e:
        logger.error(f"Failed to process incident {incident_id}: {e}")
        transition_state(incident_id, "INVESTIGATING", "FAILED", error_reason=str(e))
        raise
```

## Step 8: Deploy & Test

1. Deploy MCP server container on EC2
2. Deploy updated supervisor-agent Lambda
3. Test: `python3 chaos/iam_chaos.py revoke --target s3`
4. Invoke data-processor → fails → publishes to SNS
5. Check supervisor CloudWatch logs → should show MCP tool call results
6. Check DynamoDB `incident-context` table → should have persisted record
7. Check DynamoDB `incident-state` table → should show `CONTEXT_GATHERED` status
8. Failure test: stop MCP container, trigger chaos, invoke data-processor → verify `incident-state` shows `FAILED` with `error_reason`
9. Restore: `python3 chaos/iam_chaos.py restore`

## Token Observability

### Phase 2 — what we do now

`gather_context()` returns `(context, metrics)` — context goes to DynamoDB, metrics stay separate.

**Metrics are logged as structured JSON to CloudWatch Logs**, queryable via CloudWatch Insights:

```python
# Emitted by orchestrator.py after gather_context()
{
    "event": "token_metrics",
    "incident_id": "data-processor#2024-02-05T...",
    "agent": "supervisor",
    "metrics": {
        "token_budget": 3000,
        "raw_tokens_total": 4200,
        "raw_tokens_per_tool": {"cloudwatch_logs": 2400, "iam_policy": 1200, "lambda_config": 600},
        "final_tokens": 2800,
        "truncated": true,
        "truncation_details": { ... }
    }
}
```

Example CloudWatch Insights query:
```
fields @timestamp, metrics.raw_tokens_total, metrics.final_tokens, metrics.truncated
| filter event = "token_metrics"
| sort @timestamp desc
```

`TOKEN_BUDGET` is an env var on the supervisor Lambda — no redeploy needed to change it.

### Token estimation

```python
def estimate_tokens(data) -> int:
    """Rough estimate: len(json.dumps(data)) // 4"""
    return len(json.dumps(data, default=str)) // 4
```

### Truncation logic (`truncate_to_budget`)

Progressive truncation when raw context exceeds budget:

1. **Drop oldest log events first** — keep the most recent, they're closest to the failure
2. **Trim inline policy documents** — replace full JSON with statement Sids only
3. **Drop lambda_config** last — smallest payload, least likely to overflow

Returns `(truncated_context, truncation_details)` where `truncation_details` is:
```python
{
    "cloudwatch_logs": {"original": 1200, "final": 800, "events_dropped": 12},
    "iam_policy": {"original": 600, "final": 600, "trimmed": False},
    "lambda_config": {"original": 200, "final": 200, "dropped": False}
}
```

### Configuring the budget

```bash
aws lambda update-function-configuration \
    --function-name supervisor-agent \
    --environment "Variables={TOKEN_BUDGET=5000,MCP_SERVER_URL=http://...}" \
    --region ca-central-1
```

### Future — out of scope for Phase 2

**Dedicated `agent-metrics` DynamoDB table:**

| Attribute | Type | Key |
|-----------|------|-----|
| `incident_id` | S | Partition key — links to `incident-context` |
| `agent_name#timestamp` | S | Sort key — e.g. `supervisor#2024-02-05T...` |
| `metrics` | S | JSON-serialized metrics |
| `ttl` | N | Auto-expire after 7 days |

**Additional future fields** (added when agents make decisions):
- `diagnosis_correct` — was the root cause identified?
- `resolution_ms` — time from alert to resolution

**CloudWatch Metrics** (custom namespace) for dashboards — token usage trends, truncation rates, per-agent costs.

### Design for low-friction future wiring

The supervisor already returns metrics as a dict from `gather_context()`. To add DynamoDB persistence later:

```python
def persist_metrics(incident_id: str, agent_name: str, metrics: dict):
    dynamodb.put_item(TableName="agent-metrics", Item={
        "incident_id": {"S": incident_id},
        "agent_name#timestamp": {"S": f"{agent_name}#{datetime.now(timezone.utc).isoformat()}"},
        "metrics": {"S": json.dumps(metrics)},
        "ttl": {"N": str(int(time.time()) + 7 * 86400)}
    })
```

Call it after `gather_context()` in the orchestrator — no changes to tool code or MCP server needed.

### Experimentation workflow

1. Set `TOKEN_BUDGET=1000` → run chaos scenarios → query CloudWatch Insights
2. Set `TOKEN_BUDGET=2000` → same scenarios → compare
3. Set `TOKEN_BUDGET=5000` → same scenarios → compare
4. Set `TOKEN_BUDGET=0` (unlimited) → establishes raw baseline
5. Compare: at which budget does truncation start losing diagnostic value?

Once `agent-metrics` table exists, correlate budget size with `diagnosis_correct` and `resolution_ms`.

## Crash Recovery

### Phase 2 (manual)

- **Detect:** Query/scan `incident-state` for `status=INVESTIGATING` with stale `updated_at` (e.g. older than 15 minutes)
- **Recover:** Manually reset to `RECEIVED` and re-invoke, or inspect `incident-context` to see if context was already written
- No GSI needed at this volume; a full scan is fine

```bash
# Find stale incidents
aws dynamodb scan \
    --table-name incident-state \
    --filter-expression "#s = :status" \
    --expression-attribute-names '{"#s": "status"}' \
    --expression-attribute-values '{":status": {"S": "INVESTIGATING"}}' \
    --region ca-central-1
```

### Future (Phase 3+)

- EventBridge scheduled rule to detect stale `INVESTIGATING` records
- Auto-retry with `retry_count` field, capped at 3
- Dead-letter tracking after max retries

## Relationship Between Tables

| Table | Purpose | Owned by |
|-------|---------|----------|
| `incident-state` | Lifecycle authority — "what's happening" | Supervisor |
| `incident-context` | Evidence store — "what we found" | Supervisor (write-once) |

- Linked by shared `incident_id` (application-level join, no foreign key)
- **Invariant:** never write to `incident-context` without first writing to `incident-state`
- Schemas share only `incident_id` — no field overlap beyond the key

## Adding Future Tools

1. Add a new function in `mcp/supervisor/tools/`
2. Register it with `@app.tool()` in `server.py`
3. Rebuild and redeploy the container
4. No changes to the supervisor Lambda — the LangGraph agent (future) will discover new tools via MCP's `list_tools()`

## Why MCP Instead of Bundled Modules

- **Isolation**: Investigative AWS permissions live on EC2, not on the Lambda
- **Reusability**: Other agents (Resolver, Critic) can get their own MCP servers with different tools
- **Discoverability**: LangGraph agent can call `list_tools()` to see what's available — no hardcoded registry
- **Independent deployment**: Update tools without redeploying the Lambda
- **Aligns with CLAUDE.md architecture**: Each agent gets its own MCP server container
