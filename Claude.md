# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AWS Incident Response system that uses an agentic framework (LangGraph) to detect and autonomously resolve simulated AWS infrastructure failures. A chaos script injects faults into a target Lambda; a multi-agent system diagnoses and remediates them.

**This is a personal project. Minimize AWS spend.** Use the cheapest viable models and resources.

---

## AWS Configuration

| Setting    | Value         |
| ---------- | ------------- |
| Region     | `ca-central-1` |
| IAM Role   | `lab-lambda-baisc-role` |
| LLM        | `us.amazon.nova-2-lite-v1:0` (Bedrock inference profile) |

**All AWS deployments must use `ca-central-1`.**

---

## Target Infrastructure (the thing that breaks)

A simple **data-processing Lambda** that:
- Reads logs from S3 and prints them to CloudWatch.
- Holds IAM permissions for both **S3** and **CloudWatch Logs**.

### Existing AWS Resources

| Resource             | Identifier                          |
| -------------------- | ----------------------------------- |
| Lambda function      | `data-processor`                    |
| S3 bucket            | `lab-security-evidence-1`           |
| CloudWatch log group | `/aws/lambda/agent-trigger-message` |
| IAM role             | `lab-lambda-baisc-role`             |

---

## Chaos Script

Injects one or more of the following faults against the target Lambda:

| Fault type       | Mechanism                                                                          |
| ---------------- | ---------------------------------------------------------------------------------- |
| Permission loss  | Revokes the Lambda's IAM policy for S3, CloudWatch, or both                        |
| Throttling       | Sets the Lambda's **reserved concurrency** to `0` or `1`                           |
| Network block    | Adds a **deny-all inbound rule** on a specific port to the Lambda's security group |

---

## Agent Architecture

All agents are implemented with **LangGraph**. Every agent must document its **chain of thought** at each step.

### Communication topology

```
        Human (SNS approval)
            ▲
            │ (critical actions only)
            ▲
     ┌──────────────┐
     │  Supervisor  │  ← orchestrator; owns incident state
     └──┬───────┬───┘
        │       │
        ▼       ▼
 ┌────────┐ ┌────────┐
 │Resolver│ │Critic  │
 └────────┘ └────────┘
```

- **Supervisor Agent** — central orchestrator. Treats the Resolver and Critic as **tools** (callable sub-agents). Owns and maintains the current incident state, which it pushes to sub-agents before each invocation.
- **Resolution Agent** — proposes and executes remediations. Reports back exclusively to the Supervisor; never contacts the Critic directly.
- **Critic Agent** — reviews and must approve every action the Resolution Agent proposes before it is executed. Also reports back exclusively to the Supervisor. For **critical actions**, the Critic triggers a human-approval gate via an **SNS message** before signing off.

### Key design constraints
1. Resolution and Critic agents **never communicate directly**; all interaction is mediated by the Supervisor.
2. The Supervisor is the single source of truth for incident state.
3. Every agent must persist its reasoning / chain-of-thought so the full decision trail is auditable.

### Tool Access via MCP Servers

- Tools are accessed via **MCP (Model Context Protocol) servers**
- Each MCP server runs as a **container** on an EC2 instance (to be created)
- **Isolation**: Each agent has access to its own MCP server only — no cross-agent tool access

---

## Shared Incident State

The Supervisor shares the current incident state with sub-agents before each call. The backing store is **DynamoDB** — two tables: `incident-state` (lifecycle tracking) and `incident-context` (diagnostic evidence).

---

## Pending Decisions
- EC2 instance for MCP server containers (to be created).
