"""
Resolver Agent Lambda - Entry point for remediation proposal generation.

Receives SNS notifications from supervisor (resolver-trigger topic) with
a diagnosis payload, runs the resolver agent, and writes results to DynamoDB.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb", region_name="ca-central-1")

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8081/sse")
MCP_API_KEY = os.environ.get("MCP_API_KEY", "")


# ---------------------------------------------------------------------------
# State management (reuses incident-state table owned by supervisor)
# ---------------------------------------------------------------------------

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


def _store_audit(incident_id: str, reasoning_chain: list, token_usage: list):
    """Write reasoning chain + token usage to incident-audit table."""
    item = {
        "incident_id": {"S": incident_id},
        "agent": {"S": "resolver"},
        "created_at": {"S": datetime.now(timezone.utc).isoformat()},
        "ttl": {"N": str(int(time.time()) + 7 * 86400)},
    }
    if reasoning_chain:
        steps_json = json.dumps(reasoning_chain, indent=2, default=str)
        if len(steps_json.encode()) > 350_000:
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


def _store_proposal(incident_id: str, proposal_dict: dict):
    """Write proposal to incident-state as enrichment."""
    dynamodb.update_item(
        TableName="incident-state",
        Key={"incident_id": {"S": incident_id}},
        UpdateExpression="SET proposal = :p, updated_at = :now",
        ExpressionAttributeValues={
            ":p": {"S": json.dumps(proposal_dict, default=str)},
            ":now": {"S": datetime.now(timezone.utc).isoformat()},
        },
    )


# ---------------------------------------------------------------------------
# SNS parsing
# ---------------------------------------------------------------------------

def parse_sns_event(event: dict) -> dict:
    """Extract resolver payload from SNS event."""
    record = event["Records"][0]
    message_body = record["Sns"]["Message"]
    return json.loads(message_body)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def handler(event, context):
    logger.info(f"Resolver agent triggered: {json.dumps(event)}")

    payload = parse_sns_event(event)
    incident_id = payload.get("incident_id")
    diagnosis = payload.get("diagnosis")

    if not incident_id or not diagnosis:
        logger.error(f"Malformed resolver payload: missing incident_id or diagnosis")
        return {"statusCode": 200, "body": json.dumps({"error": "malformed payload"})}

    # Transition RESOLVING → (running agent)
    try:
        from agent import run_agent
        from shared.schemas import AgentError

        loop = asyncio.new_event_loop()
        try:
            agent_result = loop.run_until_complete(
                run_agent(diagnosis, incident_id, context)
            )
        finally:
            loop.close()

        proposal = agent_result.get("proposal") if agent_result else None
        reasoning_chain = agent_result.get("reasoning_chain", []) if agent_result else []
        token_usage = agent_result.get("token_usage", []) if agent_result else []

        _store_audit(incident_id, reasoning_chain, token_usage)

        if proposal:
            proposal_dict = proposal.model_dump()
            _store_proposal(incident_id, proposal_dict)
            logger.info(json.dumps({
                "event": "resolver_proposal_ready",
                "incident_id": incident_id,
                "fault_types": proposal_dict.get("fault_types", []),
                "action_count": len(proposal_dict.get("actions", [])),
                "llm_calls": len(token_usage),
                "total_tokens": sum(t.get("total_tokens", 0) for t in token_usage),
            }))
            transition_state(incident_id, "RESOLVING", "PROPOSED")
            return {"statusCode": 200, "body": json.dumps({
                "incident_id": incident_id,
                "status": "PROPOSED",
            })}
        else:
            transition_state(
                incident_id, "RESOLVING", "PROPOSAL_FAILED",
                error_reason="Agent produced no proposal",
            )
            return {"statusCode": 200, "body": json.dumps({
                "incident_id": incident_id,
                "status": "PROPOSAL_FAILED",
            })}

    except AgentError as e:
        logger.error(f"Agent error for {incident_id}: {e}")
        try:
            transition_state(
                incident_id, "RESOLVING", "PROPOSAL_FAILED",
                error_reason=str(e), error_category=e.category,
            )
        except Exception as t_err:
            logger.error(f"Could not mark PROPOSAL_FAILED: {t_err}")
        return {"statusCode": 200, "body": json.dumps({
            "incident_id": incident_id,
            "status": "PROPOSAL_FAILED",
            "error_category": e.category,
        })}

    except Exception as e:
        logger.error(f"Failed to process resolver for {incident_id}: {e}")
        try:
            transition_state(
                incident_id, "RESOLVING", "PROPOSAL_FAILED",
                error_reason=str(e),
            )
        except Exception as t_err:
            logger.error(f"Could not mark PROPOSAL_FAILED: {t_err}")
        raise
