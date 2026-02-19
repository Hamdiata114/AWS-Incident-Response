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
                     MCP Tool Server
                  (CloudWatch, IAM, Lambda)
                              │
                              ▼
                     Target: data-processor Lambda
```

- **Supervisor** — orchestrator Lambda; receives SNS alerts, runs a LangGraph ReAct agent to diagnose faults, persists state to DynamoDB.
- **Resolver** — proposes and executes remediations (planned).
- **Critic** — reviews actions before execution; gates critical changes through human approval via SNS (planned).
- **MCP Server** — exposes diagnostic tools (CloudWatch logs, IAM state, Lambda config) over SSE with API key auth.

## How It Works

1. Chaos script revokes IAM permissions (or throttles / blocks network) on the `data-processor` Lambda.
2. `data-processor` fails → CloudWatch alarm fires → SNS triggers the Supervisor Lambda.
3. Supervisor connects to the MCP tool server and runs a diagnostic loop (up to 5 tool calls within a token budget).
4. Agent produces a structured `Diagnosis` (root cause, severity, recommended fix) and persists it to DynamoDB.
5. Watchdog Lambda (EventBridge, every 5 min) marks stale incidents as FAILED.

## Status

| Component | Status |
|-----------|--------|
| Chaos script (IAM revoke/restore) | Done |
| Target Lambda (`data-processor`) | Done |
| MCP tool server (3 diagnostic tools) | Done |
| Supervisor agent (LangGraph ReAct) | Done |
| Orchestrator (SNS → diagnose → DynamoDB) | Done |
| Watchdog (stale incident cleanup) | Done |
| Resolver agent | Planned |
| Critic agent + human approval gate | Planned |
| EC2 deployment for MCP server | Planned |

## Project Structure

```
├── chaos/
│   ├── iam_chaos.py                # Fault injection (revoke/restore IAM)
│   └── tests/
│       ├── conftest.py
│       └── test_iam_chaos.py
├── lambda/
│   ├── data_processor/
│   │   ├── processor.py            # Target Lambda (reads S3, writes CloudWatch)
│   │   └── tests/
│   │       ├── conftest.py
│   │       └── test_processor.py
│   ├── supervisor/
│   │   ├── agent.py                # LangGraph ReAct agent (diagnosis loop)
│   │   ├── orchestrator.py         # Lambda handler + state machine
│   │   ├── schemas.py              # Pydantic models + tool schemas
│   │   ├── requirements.txt        # Supervisor Lambda dependencies
│   │   ├── package/                # Vendored deps for Lambda deployment
│   │   └── tests/
│   │       ├── conftest.py
│   │       ├── test_agent.py
│   │       ├── test_orchestrator.py
│   │       └── test_schemas.py
│   └── watchdog/
│       ├── handler.py              # Stale incident cleanup
│       └── tests/
│           ├── conftest.py
│           └── test_handler.py
├── mcp/supervisor/
│   ├── server.py                   # FastMCP server (SSE + API key auth)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tools/
│   │   ├── cloudwatch_logs.py
│   │   ├── iam_policy.py
│   │   └── lambda_config.py
│   └── tests/
│       ├── conftest.py
│       ├── test_server.py
│       ├── test_tools_cloudwatch.py
│       ├── test_tools_iam.py
│       └── test_tools_lambda_config.py
├── CLAUDE.md                       # Dev instructions for Claude Code
├── pytest.ini
├── requirements.txt                # All dependencies
└── venv/                           # Python virtual environment
```

## Tech Stack

- **Agent framework**: LangGraph + LangChain
- **LLM**: Amazon Nova Lite via Bedrock (`us.amazon.nova-2-lite-v1:0`)
- **Tool protocol**: MCP (Model Context Protocol) over SSE
- **State store**: DynamoDB
- **Testing**: pytest + moto (AWS mocking)
- **Region**: `ca-central-1`

## Running Tests

```bash
pip install -r requirements.txt
pytest lambda/supervisor/tests/ -v
```

## Running Chaos Demo

```bash
# Check current IAM state
python3 chaos/iam_chaos.py status

# Inject fault (revoke S3 permissions)
python3 chaos/iam_chaos.py revoke --target s3

# Restore permissions
python3 chaos/iam_chaos.py restore
```
