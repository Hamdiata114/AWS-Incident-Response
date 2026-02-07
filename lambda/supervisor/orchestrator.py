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
from datetime import datetime, timezone

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


def transition_state(incident_id: str, from_status: str, to_status: str, error_reason: str = None):
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

    dynamodb.update_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression=update_expr,
        ConditionExpression="#s = :from_status",
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


# ---------------------------------------------------------------------------
# Token estimation & truncation
# ---------------------------------------------------------------------------

def estimate_tokens(data) -> int:
    return len(json.dumps(data, default=str)) // 4


def truncate_to_budget(context: dict, budget: int):
    if budget <= 0:
        return context, {"skipped": True, "reason": "unlimited budget"}

    current = estimate_tokens(context)
    details = {}

    tools = context.get("tools", {})

    # 1. Drop oldest log events first
    if current > budget and "cloudwatch_logs" in tools:
        logs_data = tools["cloudwatch_logs"]
        if isinstance(logs_data, dict) and "events" in logs_data:
            original_count = len(logs_data["events"])
            while current > budget and logs_data["events"]:
                logs_data["events"].pop(0)
                current = estimate_tokens(context)
            details["cloudwatch_logs"] = {
                "events_dropped": original_count - len(logs_data["events"]),
            }

    # 2. Trim inline policy documents to Sid only
    if current > budget and "iam_policy" in tools:
        iam_data = tools["iam_policy"]
        if isinstance(iam_data, dict) and "inline_policies" in iam_data:
            for name, doc in iam_data["inline_policies"].items():
                if isinstance(doc, dict) and "Statement" in doc:
                    iam_data["inline_policies"][name] = {
                        "StatementSids": [s.get("Sid", "unnamed") for s in doc["Statement"]]
                    }
            current = estimate_tokens(context)
            details["iam_policy"] = {"trimmed": True}

    # 3. Drop lambda_config last
    if current > budget and "lambda_config" in tools:
        tools["lambda_config"] = {"dropped": True}
        details["lambda_config"] = {"dropped": True}

    return context, details


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

    raw_total = sum(raw_sizes.values())
    truncated_context, truncation_details = truncate_to_budget(context, TOKEN_BUDGET)
    final_total = estimate_tokens(truncated_context)

    metrics = {
        "token_budget": TOKEN_BUDGET,
        "raw_tokens_total": raw_total,
        "raw_tokens_per_tool": raw_sizes,
        "final_tokens": final_total,
        "truncated": raw_total > TOKEN_BUDGET if TOKEN_BUDGET > 0 else False,
        "truncation_details": truncation_details,
    }

    return truncated_context, metrics


# ---------------------------------------------------------------------------
# SNS parsing
# ---------------------------------------------------------------------------

def parse_sns_event(event: dict) -> dict:
    record = event["Records"][0]
    message_body = record["Sns"]["Message"]
    return json.loads(message_body)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, _context):
    logger.info(f"Supervisor agent triggered: {json.dumps(event)}")

    incident = parse_sns_event(event)
    incident_id = f"{incident['lambda_name']}#{incident['timestamp']}"

    # Dedup + crash recovery check
    existing = get_state(incident_id)
    if existing is None:
        write_initial_state(incident_id)
    elif existing["status"] == "RECEIVED":
        logger.info(f"Crash recovery path for {incident_id}")
    else:
        logger.info(f"Already in {existing['status']}, skipping: {incident_id}")
        return {"statusCode": 200, "body": "already handled"}

    transition_state(incident_id, "RECEIVED", "INVESTIGATING")

    try:
        context, metrics = asyncio.run(gather_context(incident, incident_id))

        logger.info(json.dumps({
            "event": "token_metrics",
            "incident_id": incident_id,
            "agent": "supervisor",
            "metrics": metrics,
        }))

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

        transition_state(incident_id, "INVESTIGATING", "CONTEXT_GATHERED")
        logger.info(f"Context gathered for {incident_id}")

        return {"statusCode": 200, "body": json.dumps({"incident_id": incident_id, "status": "CONTEXT_GATHERED"})}

    except Exception as e:
        logger.error(f"Failed to process incident {incident_id}: {e}")
        try:
            transition_state(incident_id, "INVESTIGATING", "FAILED", error_reason=str(e))
        except Exception as t_err:
            logger.error(f"Could not mark FAILED: {t_err}")
        raise
