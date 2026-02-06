# Phase 2 Design Issues

---

## Issue 1: `await` inside sync `lambda_handler`

**Labels:** bug, phase-2

### Background

`orchestrator.py` defines `lambda_handler` as a sync function (`def`) but calls `await gather_context(incident)` on line 329. AWS Lambda does not natively support async handlers.

### Problem

This is a syntax error. Python raises `SyntaxError` at import time — the Lambda won't even load, let alone run. Every invocation returns a 500 before any logic executes.

### Open Questions

- Use `asyncio.run()` inside the sync handler, or make the handler async with a wrapper?
- Does the MCP client SDK require a long-lived event loop, or is a per-invocation `asyncio.run()` fine?

### Decision

TBD

---

## Issue 2: SNS duplicate delivery crashes the Lambda

**Labels:** bug, phase-2

### Background

SNS has at-least-once delivery. Duplicate messages are expected behavior. `write_initial_state` uses `ConditionExpression: attribute_not_exists(incident_id)` for idempotency.

### Problem

When a duplicate arrives, `put_item` raises `ConditionalCheckFailedException`. The code never catches it, so the Lambda crashes. Even if caught, the next call `transition_state("RECEIVED", "INVESTIGATING")` would also fail because the status is already `INVESTIGATING` or `CONTEXT_GATHERED` from the first delivery. Duplicates should be silent no-ops, not errors.

### Open Questions

- Catch `ConditionalCheckFailedException` and return early?
- Or check if state already exists with a `get_item` first?
- Should we log duplicate deliveries as a metric?

### Decision

TBD

---

## Issue 3: `transition_state(FAILED)` in except block can itself throw

**Labels:** bug, phase-2

### Background

The `except` block in `lambda_handler` calls `transition_state(incident_id, "INVESTIGATING", "FAILED", ...)` before re-raising. This is the only path that records failures.

### Problem

If DynamoDB is throttled, unreachable, or the status is no longer `INVESTIGATING` (e.g. TTL expired, manual intervention), `transition_state` raises inside the except block. This masks the original exception — CloudWatch shows a `ConditionalCheckFailedException` or network error instead of the real failure cause. The `raise` on the next line never executes. Violates the principle that error-handling code should not itself introduce new failure modes.

### Open Questions

- Wrap in a nested `try/except` that logs but swallows the transition failure?
- Should there be a "best-effort" variant of `transition_state` for use in error paths?

### Decision

TBD

---

## Issue 4: Lambda timeout leaves state stuck at INVESTIGATING

**Labels:** design-flaw, phase-2

### Background

Lambda hard-kills the process on timeout — no exception is raised, no `finally` block runs, no `except` block executes. The `incident-state` record stays `INVESTIGATING` permanently (until TTL expires in 7 days).

### Problem

The crash recovery section detects stale `INVESTIGATING` records by `updated_at` age, using a 15-minute threshold as an example. But Lambda's max timeout is also 15 minutes. If the Lambda timeout is set to 15 minutes, the stale threshold and the timeout are identical — you'd detect the record as stale at the exact moment it might still be running. There's no guidance on what the Lambda timeout should be, or how the stale threshold should relate to it.

### Open Questions

- What should the Lambda timeout be? (MCP calls are sequential HTTP — 3 tools, likely <30s total under normal conditions)
- Should the stale threshold be `2x Lambda timeout` to avoid false positives?
- Should `updated_at` be refreshed between each MCP tool call (not just on state transitions) to give a tighter heartbeat signal?

### Decision

TBD

---

## Issue 5: Crash recovery procedure contradicts idempotency guard

**Labels:** design-flaw, phase-2

### Background

Crash recovery says: "reset to RECEIVED and re-invoke." `write_initial_state` uses `attribute_not_exists(incident_id)` so the record can only be created once.

### Problem

If you manually reset status to `RECEIVED` and then re-trigger via SNS, the Lambda calls `write_initial_state` which fails `attribute_not_exists` because the record exists. If you directly invoke the Lambda, same problem. The recovery procedure doesn't work with the idempotency guard. Either the recovery must bypass `write_initial_state`, or the function needs to handle the "record exists but status is RECEIVED" case.

### Open Questions

- Should `write_initial_state` use `attribute_not_exists(incident_id) OR status = RECEIVED` as the condition?
- Or should recovery skip `write_initial_state` entirely and only call `transition_state(RECEIVED, INVESTIGATING)`?
- Does recovery need a separate code path / entry point?

### Decision

TBD

---

## Issue 6: `TOKEN_BUDGET=0` behavior is unspecified

**Labels:** design-smell, phase-2

### Background

The experimentation workflow says `TOKEN_BUDGET=0` establishes a "raw baseline" (unlimited). `TOKEN_BUDGET` defaults to `3000` via env var. `truncate_to_budget()` receives this value.

### Problem

The behavior of `truncate_to_budget(context, 0)` is never defined. Does it skip truncation? Truncate everything to zero tokens? The function spec doesn't say. This is a latent bug — someone running the experimentation workflow will hit undefined behavior on step 4.

### Open Questions

- Should `0` mean "unlimited" (skip truncation)?
- Or use a sentinel like `-1` or `None` for unlimited, and keep `0` as an error?
- Should there be validation on Lambda startup that rejects nonsensical values?

### Decision

TBD

---

## Issue 7: Lambda-to-EC2 networking is unaddressed

**Labels:** design-flaw, blocker, phase-2

### Background

The architecture has the supervisor Lambda calling the MCP server on EC2 via HTTP to a private IP. Lambda runs outside a VPC by default. Step 2 says "allow inbound from Lambda's VPC/security group."

### Problem

A non-VPC Lambda cannot reach an EC2 private IP. There are two paths, both with significant trade-offs:

1. **Put Lambda in a VPC** — requires `AWSLambdaVPCAccessExecutionRole`, `ec2:CreateNetworkInterface` permissions (missing from Step 3b), a NAT gateway for DynamoDB access (cost + setup), and adds cold-start latency.
2. **Give EC2 a public IP** — exposes the MCP server to the internet. The server has no auth, so anyone can call the tools.

This is the single biggest deployment blocker and is not covered anywhere in the plan.

### Open Questions

- VPC Lambda or public EC2?
- If VPC: who pays for the NAT gateway? Use VPC endpoints for DynamoDB instead?
- If public: add auth (API key, mTLS) to the MCP server?
- Could we use a VPC endpoint for the MCP server instead?

### Decision

TBD

---

## Issue 8: MCP server container has no restart policy or monitoring

**Labels:** design-smell, phase-2

### Background

Step 6 deploys the MCP server as a single Docker container on a single EC2 instance. The `docker run` command has no flags for restart behavior. There is no health check beyond a one-time `curl` at deploy time.

### Problem

If the container crashes (OOM, unhandled exception, boto3 credential expiry), the MCP server is down. Every subsequent Lambda invocation fails until someone manually SSHs in and restarts. There's no alerting — the only signal is a spike in `FAILED` incidents. Single point of failure with zero automatic recovery. Violates the principle that infrastructure should self-heal for transient failures.

### Open Questions

- `--restart unless-stopped` sufficient, or use `systemd` / ECS for proper process supervision?
- Add a `/health` endpoint and a CloudWatch alarm on EC2 status checks?
- Is a single EC2 instance acceptable for Phase 2, or should we use ECS from the start?

### Decision

TBD

---

## Issue 9: No version pins in MCP server `requirements.txt`

**Labels:** design-smell, phase-2

### Background

`mcp/supervisor/requirements.txt` contains only `mcp` and `boto3` with no version constraints.

### Problem

Builds are non-reproducible. A `docker build` today and six months from now could pull completely different SDK versions. The MCP Python SDK is young and its API surface is still changing — a breaking change in `mcp` silently breaks the server on next deploy. Violates the principle that builds should be deterministic.

### Open Questions

- Pin exact versions (`mcp==x.y.z`) or use compatible ranges (`mcp~=x.y`)?
- Add a `requirements.lock` or use `pip freeze` output?
- Pin `python:3.12-slim` to a specific digest in the Dockerfile too?

### Decision

TBD

---

## Issue 10: `error_reason` stores unbounded `str(e)`

**Labels:** design-smell, phase-2

### Background

When the Lambda fails, `transition_state` stores `error_reason=str(e)` in the `incident-state` table.

### Problem

`str(e)` for AWS SDK exceptions can include full API error responses, and some exceptions produce multi-KB strings. While DynamoDB string attributes have no per-attribute limit, the entire item must be < 400KB. More practically, a raw exception string is noisy and hard to query. Future consumers of `incident-state` would need to parse unstructured error text.

### Open Questions

- Truncate to a fixed length (e.g. 500 chars)?
- Store a structured error (`{"type": "ConnectionError", "message": "...truncated..."}`)?
- Is the raw string fine for Phase 2 and we clean it up later?

### Decision

TBD

---

## Issue 11: Crash recovery scan requires `dynamodb:Scan` — not in any IAM role

**Labels:** design-smell, phase-2

### Background

The crash recovery section provides an `aws dynamodb scan` CLI command to find stale `INVESTIGATING` records. The `supervisor-agent-role` only has `PutItem`, `GetItem`, `Query`, `UpdateItem`.

### Problem

The scan command won't work with the supervisor role. It's presumably run by an operator using their own credentials, but the doc doesn't say that. If someone tries to automate recovery using the supervisor role (natural next step for Phase 3), they'll hit `AccessDenied`.

### Open Questions

- Add `dynamodb:Scan` to `supervisor-agent-role` preemptively?
- Or explicitly document that crash recovery uses operator credentials?
- Will Phase 3 automation need its own role?

### Decision

TBD

---

## Issue 12: `get_recent_logs` silently returns empty results for custom log groups

**Labels:** design-smell, phase-2

### Background

`get_recent_logs` derives the log group as `/aws/lambda/{lambda_name}`. This matches the default CloudWatch naming convention.

### Problem

If the target Lambda uses a custom log group, `filter_log_events` returns zero events — no error, just empty results. The supervisor proceeds with missing evidence and could misdiagnose. For `data-processor` in Phase 2 this works, but it's a silent failure waiting for Phase 3+ when more Lambdas are added. Violates fail-fast: the tool should distinguish "no logs exist" from "wrong log group."

### Open Questions

- Validate the log group exists (call `describe_log_groups` first) and error if not found?
- Accept log group as an optional parameter, fall back to convention?
- Out of scope for Phase 2 since only `data-processor` is targeted?

### Decision

TBD

---

## Issue 13: `get_iam_state` IAM scope is hardcoded to `data-processor`

**Labels:** design-smell, phase-2

### Background

`supervisor-mcp-role` restricts `lambda:GetFunction` to `arn:aws:lambda:ca-central-1:534321188934:function:data-processor`. `get_iam_state` calls `GetFunction` with whatever `lambda_name` is passed.

### Problem

If a malformed SNS message or future multi-Lambda support passes a different `lambda_name`, the tool gets `AccessDenied` from AWS. The error surfaces as a generic MCP tool failure — no clear signal that the issue is IAM scoping vs. an actual problem. The tool doesn't validate its input against expected targets.

### Open Questions

- Add input validation in the tool (allowlist of supported Lambda names)?
- Widen the IAM scope to `function:*` to support future Lambdas?
- Fine for Phase 2 since only `data-processor` is in scope?

### Decision

TBD
