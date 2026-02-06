# Phase 2: Supervisor Context Enrichment

## Overview

When supervisor-agent receives an incident via SNS, it gathers diagnostic context from AWS (logs, IAM state, Lambda config), persists it to DynamoDB, then acts. The supervisor does the gathering — not data-processor — because data-processor is the one losing permissions.

## Decisions

- **Separate IAM role** for supervisor (`supervisor-agent-role`)
- **Sequential** collector execution
- **DynamoDB** persistence for enriched context

## Architecture

```
SNS incident arrives
    → orchestrator.py parses alert
    → enrich_incident(alert)
        → registry looks up collectors for error_type
        → runs: cloudwatch_logs, iam_policy, lambda_config
        → truncates to token budget
    → persist to DynamoDB (incident-context table)
    → enriched context ready for agents (future)
```

## File Structure

```
lambda/supervisor/
    orchestrator.py                  # Modify: call enrichment + DynamoDB persist
    context/
        __init__.py                  # enrich_incident() entry point
        models.py                    # IncidentAlert, CollectorResult, EnrichedContext
        registry.py                  # error_type → collector mapping
        truncation.py                # Token-budget-aware truncation
        collectors/
            __init__.py              # Wires registrations
            cloudwatch_logs.py       # Recent logs from failing Lambda
            iam_policy.py            # Current IAM policy state
            lambda_config.py         # Lambda function metadata
```

---

## Step 1: Create DynamoDB Table

**Table:** `incident-context` in ca-central-1

| Attribute | Type | Key |
|-----------|------|-----|
| `incident_id` | S | Partition key — `{lambda_name}#{timestamp}` |
| `error_type` | S | — |
| `enriched_context` | S | JSON-serialized EnrichedContext |
| `created_at` | S | ISO timestamp |
| `ttl` | N | Auto-expire after 7 days |

Enable TTL on the `ttl` attribute.

## Step 2: Create Supervisor IAM Role

Create `supervisor-agent-role` with:

**Trust policy:** Lambda service

**Managed policy:** `AWSLambdaBasicExecutionRole`

**Inline policy `supervisor-context-enrichment`:**

| Action | Scoped Resource |
|--------|----------------|
| `logs:FilterLogEvents`, `logs:DescribeLogStreams` | `arn:aws:logs:ca-central-1:534321188934:log-group:/aws/lambda/*` |
| `lambda:GetFunction`, `lambda:GetFunctionConfiguration` | `arn:aws:lambda:ca-central-1:534321188934:function:data-processor` |
| `iam:ListAttachedRolePolicies`, `iam:ListRolePolicies`, `iam:GetRolePolicy` | `arn:aws:iam::534321188934:role/lab-lambda-baisc-role` |

**Inline policy `supervisor-dynamodb-access`:**
- `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Query` on `arn:aws:dynamodb:ca-central-1:534321188934:table/incident-context`

## Step 3: Update Supervisor Lambda Role

Update `supervisor-agent` Lambda to use `supervisor-agent-role` instead of `lab-lambda-baisc-role`.

## Step 4: Implement `context/models.py`

Three dataclasses:
- **IncidentAlert** — parsed from SNS (`error_type`, `error_message`, `error_code`, `lambda_name`, `timestamp`)
- **CollectorResult** — one collector's output (`source`, `data` dict, optional `error`, `truncated` flag)
- **EnrichedContext** — assembled result (`incident`, list of `CollectorResult`, `enrichment_timestamp`, `estimated_tokens()`, `to_dict()`)

## Step 5: Implement `context/registry.py`

Strategy pattern:
- `register(error_type, collector_fn)` — adds collector for a specific error type
- `register_default(collector_fn)` — adds collector for all types
- `get_collectors(error_type)` — returns ordered list
- Collector signature: `(alert: IncidentAlert, session: boto3.Session) -> CollectorResult`

## Step 6: Implement `context/truncation.py`

- `MAX_CONTEXT_TOKENS = 3000`
- `truncate_string(s, max_chars=500)` — caps individual strings
- `enforce_token_budget(context, budget)` — progressively trims: oldest logs first → policy docs → lambda config last

## Step 7: Implement Collectors

**a) `collectors/lambda_config.py` — `collect_lambda_config()`**
- Calls `get_function_configuration` for `alert.lambda_name`
- Returns subset: FunctionName, Runtime, Handler, Role, MemorySize, Timeout, LastModified, State, ReservedConcurrentExecutions
- **Strips Environment.Variables** for security

**b) `collectors/iam_policy.py` — `collect_iam_state()`**
- Gets role name from `lambda:GetFunction` (not hardcoded)
- Lists attached managed policies (ARNs only)
- Fetches all inline policy documents (full JSON)
- Returns `CollectorResult(source="iam_policy", data={"role_name": ..., "inline_policies": {...}, "attached_policies": [...]})`

**c) `collectors/cloudwatch_logs.py` — `collect_recent_logs()`**
- Log group: `/aws/lambda/{alert.lambda_name}` (derived, not hardcoded)
- Uses `filter_log_events`, `startTime` = incident timestamp - 10min, `limit=30`
- Each message truncated to 500 chars
- Returns `CollectorResult(source="cloudwatch_logs", data={"log_group": ..., "events": [...]})`

## Step 8: Wire Collector Registration (`collectors/__init__.py`)

```python
register_default(collect_lambda_config)           # all incident types
register("S3AccessError", collect_recent_logs)
register("S3AccessError", collect_iam_state)
register("CloudWatchAccessError", collect_recent_logs)
register("CloudWatchAccessError", collect_iam_state)
```

## Step 9: Implement Entry Point (`context/__init__.py`)

`enrich_incident(alert, token_budget=3000)`:
- Creates boto3 session (ca-central-1)
- Runs each collector sequentially; captures failures as CollectorResult with error field
- Calls `enforce_token_budget()`
- Returns `EnrichedContext`

## Step 10: Update `orchestrator.py`

After parsing the SNS message:

```python
# Enrich
alert = IncidentAlert(**incident)
enriched = enrich_incident(alert)
logger.info(f"Enriched context ({enriched.estimated_tokens()} est. tokens)")

# Persist to DynamoDB
dynamodb.put_item(TableName="incident-context", Item={
    "incident_id": {"S": f"{alert.lambda_name}#{alert.timestamp}"},
    "error_type": {"S": alert.error_type},
    "enriched_context": {"S": json.dumps(enriched.to_dict(), default=str)},
    "created_at": {"S": datetime.now(timezone.utc).isoformat()},
    "ttl": {"N": str(int(time.time()) + 7 * 86400)}
})
```

## Step 11: Deploy & Test

1. Zip supervisor-agent with all new files (orchestrator.py + context/ package)
2. Deploy to Lambda
3. Test: `python3 chaos/iam_chaos.py revoke --target s3`
4. Invoke data-processor → should fail and publish to SNS
5. Check supervisor CloudWatch logs for enriched context
6. Check DynamoDB `incident-context` table for persisted record
7. Restore: `python3 chaos/iam_chaos.py restore`

## Adding Future Incident Types

1. Create `collectors/new_collector.py`
2. Add `register("NewErrorType", new_collector_fn)` in `collectors/__init__.py`
3. No changes to orchestrator or enrichment pipeline
