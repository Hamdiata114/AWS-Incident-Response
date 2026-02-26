# AWS Incident Response Agents

Autonomous incident response system that detects and remediates AWS infrastructure failures using a multi-agent architecture built on LangGraph. A chaos script breaks a Lambda; the agents diagnose and fix it.

## Architecture

```
  CloudWatch Alarm → SNS → Supervisor Lambda
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              Resolver Agent       Critic Agent
                    │                   │
                    └─────────┬─────────┘
                              ▼
                     MCP Tool Servers (EC2)
              ┌──────────────┴──────────────┐
              ▼                             ▼
     Supervisor MCP (:8080)        Resolver MCP (:8081)
     (CloudWatch, IAM, Lambda)     (IAM baseline, Concurrency)
              │
              ▼
     Target: data-processor Lambda
```

- **Supervisor** — orchestrator Lambda; receives SNS alerts, runs a LangGraph ReAct agent to diagnose faults, persists state to DynamoDB, hands off to Resolver via SNS.
- **Resolver** — receives diagnosis via SNS, proposes concrete remediations (exact boto3 API calls) by comparing current state against known-good baselines.
- **Critic** — reviews actions before execution; gates critical changes through human approval via SNS (planned).
- **Watchdog** — EventBridge-triggered Lambda (every 5 min) that marks stale incidents as FAILED and retries PROPOSAL_FAILED incidents via SNS.
- **MCP Servers** — two Docker containers on EC2 exposing tools over SSE with API key auth.

## How It Works

1. Chaos script revokes IAM permissions (or throttles / blocks network) on the `data-processor` Lambda.
2. `data-processor` fails → publishes to `incident-alerts` SNS → triggers the Supervisor Lambda.
3. Supervisor connects to the MCP tool server and runs a diagnostic loop (up to 12 tool calls within a token budget).
4. Agent produces a structured `Diagnosis` (root cause, fault types, severity, evidence, remediation plan) and persists it to DynamoDB.
5. Supervisor publishes diagnosis to `resolver-trigger` SNS → triggers the Resolver Lambda.
6. Resolver queries its MCP tools (IAM baseline comparison, concurrency check), proposes exact AWS API calls, and writes the proposal to DynamoDB.
7. Incident transitions: `RECEIVED → INVESTIGATING → DIAGNOSED → RESOLVING → PROPOSED`.
8. Watchdog retries `PROPOSAL_FAILED` incidents (max 2 retries) and marks stale `INVESTIGATING` incidents as `FAILED`.

## Status

| Component | Status |
|-----------|--------|
| Chaos script (IAM revoke/restore) | Done |
| Target Lambda (`data-processor`) | Done |
| Supervisor MCP server (3 diagnostic tools) | Deployed (EC2 :8080) |
| Resolver MCP server (2 remediation tools) | Deployed (EC2 :8081) |
| Supervisor agent (LangGraph ReAct) | Deployed |
| Orchestrator (SNS → diagnose → DynamoDB → resolver handoff) | Deployed |
| Resolver agent (LangGraph proposal loop) | Deployed |
| Watchdog (stale cleanup + retry) | Deployed |
| Critic agent + human approval gate | Planned |

## AWS Resources

| Resource | Identifier |
|----------|------------|
| Supervisor Lambda | `supervisor-agent` |
| Resolver Lambda | `resolver-agent` |
| Watchdog Lambda | `incident-watchdog` |
| Target Lambda | `data-processor` |
| EC2 (MCP servers) | `3.99.16.1` (Elastic IP) |
| DynamoDB tables | `incident-state`, `incident-context`, `incident-audit` |
| SNS topics | `incident-alerts`, `resolver-trigger` |
| Region | `ca-central-1` |

## Project Structure

```
├── chaos/
│   ├── iam_chaos.py                # Fault injection (revoke/restore IAM)
│   └── tests/
├── config/
│   ├── __init__.py
│   └── baseline.py                 # Known-good IAM policy baseline
├── lambda/
│   ├── data_processor/
│   │   └── processor.py            # Target Lambda (reads S3, writes CloudWatch)
│   ├── supervisor/
│   │   ├── agent.py                # LangGraph ReAct agent (diagnosis loop)
│   │   ├── orchestrator.py         # Lambda handler + state machine + resolver handoff
│   │   ├── schemas.py              # Pydantic models + tool schemas
│   │   └── requirements.txt
│   ├── resolver/
│   │   ├── agent.py                # LangGraph agent (remediation proposal)
│   │   ├── handler.py              # Lambda handler (SNS → propose → DynamoDB)
│   │   ├── schemas.py              # Proposal models + tool schemas
│   │   └── requirements.txt
│   ├── watchdog/
│   │   └── handler.py              # Stale incident cleanup + retry
│   └── shared/
│       ├── schemas.py              # AgentError, TokenUsage, ToolProvider
│       └── agent_utils.py          # Error classification, deadline check, validation
├── mcp/
│   ├── supervisor/
│   │   ├── server.py               # FastMCP server (SSE + auth, port 8080)
│   │   ├── Dockerfile
│   │   └── tools/
│   │       ├── cloudwatch_logs.py
│   │       ├── iam_policy.py
│   │       └── lambda_config.py
│   └── resolver/
│       ├── server.py               # FastMCP server (SSE + auth, port 8081)
│       ├── Dockerfile
│       └── tools/
│           ├── iam_baseline.py     # Compare current vs baseline IAM
│           └── concurrency.py      # Check reserved concurrency
├── CLAUDE.md
├── pytest.ini
└── requirements.txt
```

## Tech Stack

- **Agent framework**: LangGraph + LangChain
- **LLM**: Amazon Nova Lite via Bedrock (`us.amazon.nova-2-lite-v1:0`)
- **Tool protocol**: MCP (Model Context Protocol) over SSE
- **State store**: DynamoDB
- **Compute**: Lambda (agents), EC2 (MCP servers in Docker)
- **Messaging**: SNS (inter-agent handoff)
- **Testing**: pytest + moto (AWS mocking)
- **Region**: `ca-central-1`

## Running Tests

```bash
pip install -r requirements.txt
pytest lambda/supervisor/tests/ -v
pytest lambda/resolver/tests/ -v
```

## Running Chaos Demo

```bash
# Check current IAM state
python3 chaos/iam_chaos.py status

# Inject fault (revoke S3 permissions)
python3 chaos/iam_chaos.py revoke --target s3

# Trigger the pipeline
aws lambda invoke --function-name data-processor --region ca-central-1 /dev/stdout

# Monitor incident state
aws dynamodb scan --table-name incident-state --region ca-central-1 \
  --query 'Items[*].{id:incident_id.S,status:status.S}' --output table

# Restore permissions
python3 chaos/iam_chaos.py restore
```
