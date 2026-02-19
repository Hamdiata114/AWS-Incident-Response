"""
Supervisor Agent Lambda - Entry point for incident response.

Receives SNS notifications when data-processor encounters errors,
gathers diagnostic context via MCP tools, and persists results to DynamoDB.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from mcp import ClientSession
from mcp.client.sse import sse_client

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb", region_name="ca-central-1")

MCP_SERVER_URL = os.environ["MCP_SERVER_URL"]
MCP_API_KEY = os.environ["MCP_API_KEY"]
TOKEN_BUDGET = int(os.environ.get("TOKEN_BUDGET", "6000"))


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def get_state(incident_id: str) -> dict | None:
    resp = dynamodb.get_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
    )
    item = resp.get("Item")
    if not item:
        return None
    return {k: v.get("S", v.get("N")) for k, v in item.items()}


def write_initial_state(incident_id: str):
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


def touch_updated_at(incident_id: str):
    dynamodb.update_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression="SET updated_at = :now",
        ExpressionAttributeValues={":now": {"S": datetime.now(timezone.utc).isoformat()}},
    )


def transition_state(
    incident_id: str,
    from_status: str,
    to_status: str,
    error_reason: str = None,
    error_category: str = None,
):
    now = datetime.now(timezone.utc).isoformat()
    update_expr = "SET #s = :to_status, updated_at = :now"
    expr_values = {
        ":from_status": {"S": from_status},
        ":to_status": {"S": to_status},
        ":now": {"S": now},
    }
    expr_names = {"#s": "status"}

    if error_reason:
        update_expr += ", error_reason = :err"
        expr_values[":err"] = {"S": str(error_reason)[:500]}

    if error_category:
        update_expr += ", error_category = :ecat"
        expr_values[":ecat"] = {"S": error_category}

    dynamodb.update_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression=update_expr,
        ConditionExpression="#s = :from_status",
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


# ---------------------------------------------------------------------------
# Token estimation & truncation (Split A)
# ---------------------------------------------------------------------------

def estimate_tokens(data) -> int:
    return len(json.dumps(data, default=str)) // 4


def _drop_oldest_logs(context: dict, budget: int) -> dict:
    """Drop oldest log events until under budget. Returns truncation details."""
    details = {}
    tools = context.get("tools", {})
    if "cloudwatch_logs" not in tools:
        return details
    logs_data = tools["cloudwatch_logs"]
    if not isinstance(logs_data, dict) or "events" not in logs_data:
        return details

    current = estimate_tokens(context)
    if current <= budget:
        return details

    original_count = len(logs_data["events"])
    while current > budget and logs_data["events"]:
        logs_data["events"].pop(0)
        current = estimate_tokens(context)
    details["cloudwatch_logs"] = {
        "events_dropped": original_count - len(logs_data["events"]),
    }
    return details


def _trim_iam_to_sids(context: dict, budget: int) -> dict:
    """Replace inline policy docs with Sid lists. Returns truncation details."""
    details = {}
    tools = context.get("tools", {})
    if "iam_policy" not in tools:
        return details
    iam_data = tools["iam_policy"]
    if not isinstance(iam_data, dict) or "inline_policies" not in iam_data:
        return details

    current = estimate_tokens(context)
    if current <= budget:
        return details

    for name, doc in iam_data["inline_policies"].items():
        if isinstance(doc, dict) and "Statement" in doc:
            iam_data["inline_policies"][name] = {
                "StatementSids": [s.get("Sid", "unnamed") for s in doc["Statement"]]
            }
    details["iam_policy"] = {"trimmed": True}
    return details


def _drop_lambda_config(context: dict, budget: int) -> dict:
    """Replace lambda_config with dropped flag. Returns truncation details."""
    details = {}
    tools = context.get("tools", {})
    if "lambda_config" not in tools:
        return details

    current = estimate_tokens(context)
    if current <= budget:
        return details

    tools["lambda_config"] = {"dropped": True}
    details["lambda_config"] = {"dropped": True}
    return details


def truncate_to_budget(context: dict, budget: int):
    if budget <= 0:
        return context, {"skipped": True, "reason": "unlimited budget"}

    current = estimate_tokens(context)
    if current <= budget:
        return context, {}

    details = {}
    details.update(_drop_oldest_logs(context, budget))
    details.update(_trim_iam_to_sids(context, budget))
    details.update(_drop_lambda_config(context, budget))
    return context, details


# ---------------------------------------------------------------------------
# Metrics (Split B)
# ---------------------------------------------------------------------------

def _compute_metrics(raw_sizes: dict, token_budget: int, final_tokens: int, truncation_details: dict) -> dict:
    raw_total = sum(raw_sizes.values())
    return {
        "token_budget": token_budget,
        "raw_tokens_total": raw_total,
        "raw_tokens_per_tool": raw_sizes,
        "final_tokens": final_tokens,
        "truncated": raw_total > token_budget if token_budget > 0 else False,
        "truncation_details": truncation_details,
    }


# ---------------------------------------------------------------------------
# MCP context gathering
# ---------------------------------------------------------------------------

async def gather_context(incident: dict, incident_id: str) -> tuple[dict, dict]:
    lambda_name = incident["lambda_name"]
    context = {"incident": incident, "tools": {}}
    raw_sizes = {}

    headers = {"Authorization": f"Bearer {MCP_API_KEY}"}

    async with sse_client(MCP_SERVER_URL, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            logs = await session.call_tool("tool_get_recent_logs", {"lambda_name": lambda_name})
            logs_data = json.loads(logs.content[0].text) if logs.content else {}
            context["tools"]["cloudwatch_logs"] = logs_data
            raw_sizes["cloudwatch_logs"] = estimate_tokens(logs_data)
            touch_updated_at(incident_id)

            iam = await session.call_tool("tool_get_iam_state", {"lambda_name": lambda_name})
            iam_data = json.loads(iam.content[0].text) if iam.content else {}
            context["tools"]["iam_policy"] = iam_data
            raw_sizes["iam_policy"] = estimate_tokens(iam_data)
            touch_updated_at(incident_id)

            config = await session.call_tool("tool_get_lambda_config", {"lambda_name": lambda_name})
            config_data = json.loads(config.content[0].text) if config.content else {}
            context["tools"]["lambda_config"] = config_data
            raw_sizes["lambda_config"] = estimate_tokens(config_data)

    truncated_context, truncation_details = truncate_to_budget(context, TOKEN_BUDGET)
    final_total = estimate_tokens(truncated_context)
    metrics = _compute_metrics(raw_sizes, TOKEN_BUDGET, final_total, truncation_details)

    return truncated_context, metrics


# ---------------------------------------------------------------------------
# SNS parsing
# ---------------------------------------------------------------------------

def parse_sns_event(event: dict) -> dict:
    record = event["Records"][0]
    message_body = record["Sns"]["Message"]
    return json.loads(message_body)


# ---------------------------------------------------------------------------
# Dedup / crash recovery (Split C)
# ---------------------------------------------------------------------------

def _dedup_or_recover(incident_id: str) -> str | None:
    """Return 'skip' if already handled, None if should proceed."""
    existing = get_state(incident_id)
    if existing is None:
        write_initial_state(incident_id)
        return None
    if existing["status"] == "RECEIVED":
        logger.info(f"Crash recovery path for {incident_id}")
        return None
    if existing["status"] == "INVESTIGATING":
        updated_at = datetime.fromisoformat(existing["updated_at"])
        stale_threshold = datetime.now(timezone.utc) - timedelta(minutes=5)
        if updated_at < stale_threshold:
            logger.info(f"Stale INVESTIGATING, re-entering: {incident_id}")
            transition_state(incident_id, "INVESTIGATING", "RECEIVED")
            return None
        else:
            logger.info(f"INVESTIGATING and active, skipping: {incident_id}")
            return "skip"
    logger.info(f"Already in {existing['status']}, skipping: {incident_id}")
    return "skip"


def _store_audit(incident_id: str, reasoning_chain: list, token_usage: list):
    """Write reasoning chain + token usage to incident-audit table."""
    item = {
        "incident_id": {"S": incident_id},
        "created_at": {"S": datetime.now(timezone.utc).isoformat()},
        "ttl": {"N": str(int(time.time()) + 7 * 86400)},
    }
    if reasoning_chain:
        # Store each step as a separate readable entry
        steps_json = json.dumps(reasoning_chain, indent=2, default=str)
        if len(steps_json.encode()) > 350_000:
            # Keep first step (incident) and last 3 steps (diagnosis)
            reasoning_chain = reasoning_chain[:1] + reasoning_chain[-3:]
            steps_json = json.dumps(reasoning_chain, indent=2, default=str)
            item["reasoning_truncated"] = {"BOOL": True}
        item["reasoning_chain"] = {"S": steps_json}
        item["step_count"] = {"N": str(len(reasoning_chain))}
    if token_usage:
        total = sum(t.get("total_tokens", 0) for t in token_usage)
        item["token_usage"] = {"S": json.dumps(token_usage, default=str)}
        item["total_tokens"] = {"N": str(total)}
        item["llm_calls"] = {"N": str(len(token_usage))}
    dynamodb.put_item(TableName="incident-audit", Item=item)


def _store_context(incident_id: str, incident: dict, context: dict):
    """Write enriched context to incident-context table."""
    dynamodb.put_item(
        TableName="incident-context",
        Item={
            "incident_id": {"S": incident_id},
            "error_type": {"S": incident.get("error_type", "unknown")},
            "enriched_context": {"S": json.dumps(context, default=str)},
            "created_at": {"S": datetime.now(timezone.utc).isoformat()},
            "ttl": {"N": str(int(time.time()) + 7 * 86400)},
        },
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info(f"Supervisor agent triggered: {json.dumps(event)}")

    incident = parse_sns_event(event)

    try:
        incident_id = f"{incident['lambda_name']}#{incident['timestamp']}"
    except KeyError as e:
        incident_id = f"unknown#{uuid.uuid4()}"
        logger.error(f"Malformed payload, missing {e}. Using fallback ID: {incident_id}")
        write_initial_state(incident_id)
        transition_state(
            incident_id, "RECEIVED", "FAILED",
            error_reason=f"Malformed SNS payload: missing {e}",
        )
        return {"statusCode": 200, "body": json.dumps({"incident_id": incident_id, "status": "FAILED"})}

    result = _dedup_or_recover(incident_id)
    if result == "skip":
        return {"statusCode": 200, "body": "already handled"}

    transition_state(incident_id, "RECEIVED", "INVESTIGATING")

    try:
        from agent import run_agent
        from schemas import AgentError

        loop = asyncio.new_event_loop()
        try:
            agent_result = loop.run_until_complete(run_agent(incident, incident_id, context))
        finally:
            loop.close()

        diagnosis = agent_result.get("diagnosis") if agent_result else None
        reasoning_chain = agent_result.get("reasoning_chain", []) if agent_result else []
        token_usage = agent_result.get("token_usage", []) if agent_result else []

        if diagnosis:
            _store_context(incident_id, incident, {"diagnosis": diagnosis.model_dump()})
            _store_audit(incident_id, reasoning_chain, token_usage)
            tools_called = [
                e["tool_calls"][0]["name"] for e in reasoning_chain
                if e.get("tool_calls")
            ]
            logger.info(json.dumps({
                "event": "agent_reasoning_summary",
                "incident_id": incident_id,
                "tools_called": tools_called,
                "fault_types": diagnosis.fault_types,
                "root_cause": diagnosis.root_cause,
                "severity": diagnosis.severity,
                "llm_calls": len(token_usage),
                "total_tokens": sum(t.get("total_tokens", 0) for t in token_usage),
            }))
            transition_state(incident_id, "INVESTIGATING", "DIAGNOSED")
            logger.info(f"Diagnosis complete for {incident_id}")
            return {"statusCode": 200, "body": json.dumps({
                "incident_id": incident_id,
                "status": "DIAGNOSED",
                "root_cause": diagnosis.root_cause,
            })}
        else:
            transition_state(
                incident_id, "INVESTIGATING", "FAILED",
                error_reason="Agent produced no diagnosis",
            )
            return {"statusCode": 200, "body": json.dumps({
                "incident_id": incident_id, "status": "FAILED",
            })}

    except AgentError as e:
        logger.error(f"Agent error for {incident_id}: {e}")
        try:
            transition_state(
                incident_id, "INVESTIGATING", "FAILED",
                error_reason=str(e), error_category=e.category,
            )
        except Exception as t_err:
            logger.error(f"Could not mark FAILED: {t_err}")
        return {"statusCode": 200, "body": json.dumps({
            "incident_id": incident_id, "status": "FAILED", "error_category": e.category,
        })}

    except Exception as e:
        logger.error(f"Failed to process incident {incident_id}: {e}")
        try:
            transition_state(incident_id, "INVESTIGATING", "FAILED", error_reason=str(e))
        except Exception as t_err:
            logger.error(f"Could not mark FAILED: {t_err}")
        raise
