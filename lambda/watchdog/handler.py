"""
Stale Incident Watchdog — EventBridge-triggered Lambda (every 5 min).

Scans incident-state for:
1. INVESTIGATING incidents older than 10 min → FAILED
2. PROPOSAL_FAILED incidents older than 5 min → retry via SNS (max 2 retries)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb", region_name="ca-central-1")
sns = boto3.client("sns", region_name="ca-central-1")

STALE_THRESHOLD_MINUTES = 10
RETRY_THRESHOLD_MINUTES = 5
MAX_RETRIES = 2
RESOLVER_TOPIC_ARN = os.environ.get(
    "RESOLVER_TOPIC_ARN",
    "arn:aws:sns:ca-central-1:534321188934:resolver-trigger",
)


def scan_stale_incidents(dynamodb_client, cutoff: str) -> list:
    """DynamoDB scan for INVESTIGATING incidents older than *cutoff*."""
    resp = dynamodb_client.scan(
        TableName="incident-state",
        FilterExpression="#s = :investigating AND updated_at < :cutoff",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":investigating": {"S": "INVESTIGATING"},
            ":cutoff": {"S": cutoff},
        },
    )
    return resp.get("Items", [])


def transition_to_failed(dynamodb_client, incident_id: str) -> bool:
    """Transition a single incident to FAILED. Returns False if already transitioned."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamodb_client.update_item(
            TableName="incident-state",
            Key={"incident_id": {"S": incident_id}},
            UpdateExpression="SET #s = :failed, updated_at = :now, error_reason = :err",
            ConditionExpression="#s = :investigating",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":failed": {"S": "FAILED"},
                ":investigating": {"S": "INVESTIGATING"},
                ":now": {"S": now},
                ":err": {"S": "stale watchdog timeout"},
            },
        )
        logger.info(f"Transitioned stale incident to FAILED: {incident_id}")
        return True
    except dynamodb_client.exceptions.ConditionalCheckFailedException:
        logger.info(f"Incident already transitioned, skipping: {incident_id}")
        return False


def scan_failed_proposals(dynamodb_client, cutoff: str) -> list:
    """DynamoDB scan for PROPOSAL_FAILED incidents older than *cutoff*."""
    resp = dynamodb_client.scan(
        TableName="incident-state",
        FilterExpression="#s = :pf AND updated_at < :cutoff",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":pf": {"S": "PROPOSAL_FAILED"},
            ":cutoff": {"S": cutoff},
        },
    )
    return resp.get("Items", [])


def retry_proposal(dynamodb_client, sns_client, item: dict) -> bool:
    """Re-publish to resolver-trigger if under MAX_RETRIES. Returns True if retried."""
    incident_id = item["incident_id"]["S"]
    retry_count = int(item.get("retry_count", {}).get("N", "0"))

    if retry_count >= MAX_RETRIES:
        logger.info(f"Max retries reached for {incident_id}, marking FAILED")
        now = datetime.now(timezone.utc).isoformat()
        try:
            dynamodb_client.update_item(
                TableName="incident-state",
                Key={"incident_id": {"S": incident_id}},
                UpdateExpression="SET #s = :failed, updated_at = :now, error_reason = :err",
                ConditionExpression="#s = :pf",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":failed": {"S": "FAILED"},
                    ":pf": {"S": "PROPOSAL_FAILED"},
                    ":now": {"S": now},
                    ":err": {"S": f"max retries ({MAX_RETRIES}) exhausted"},
                },
            )
        except dynamodb_client.exceptions.ConditionalCheckFailedException:
            pass
        return False

    # Transition back to RESOLVING and re-publish
    now = datetime.now(timezone.utc).isoformat()
    try:
        dynamodb_client.update_item(
            TableName="incident-state",
            Key={"incident_id": {"S": incident_id}},
            UpdateExpression="SET #s = :resolving, updated_at = :now, retry_count = :rc",
            ConditionExpression="#s = :pf",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":resolving": {"S": "RESOLVING"},
                ":pf": {"S": "PROPOSAL_FAILED"},
                ":now": {"S": now},
                ":rc": {"N": str(retry_count + 1)},
            },
        )
    except dynamodb_client.exceptions.ConditionalCheckFailedException:
        logger.info(f"Incident {incident_id} already transitioned, skipping retry")
        return False

    # Re-read diagnosis from incident-context if available, otherwise pass minimal payload
    diagnosis = {}
    try:
        ctx_resp = dynamodb_client.get_item(
            TableName="incident-context",
            Key={"incident_id": {"S": incident_id}},
        )
        ctx_item = ctx_resp.get("Item")
        if ctx_item and "enriched_context" in ctx_item:
            enriched = json.loads(ctx_item["enriched_context"]["S"])
            diagnosis = enriched.get("diagnosis", {})
    except Exception as e:
        logger.warning(f"Could not read diagnosis for retry: {e}")

    sns_client.publish(
        TopicArn=RESOLVER_TOPIC_ARN,
        Message=json.dumps({"incident_id": incident_id, "diagnosis": diagnosis}),
    )
    logger.info(f"Retried resolver for {incident_id} (attempt {retry_count + 1})")
    return True


def handler(event, context):
    # 1. Stale INVESTIGATING → FAILED
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_THRESHOLD_MINUTES)).isoformat()
    stale_items = scan_stale_incidents(dynamodb, stale_cutoff)
    logger.info(f"Found {len(stale_items)} stale incidents")
    for item in stale_items:
        incident_id = item["incident_id"]["S"]
        transition_to_failed(dynamodb, incident_id)

    # 2. PROPOSAL_FAILED → retry via SNS
    retry_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=RETRY_THRESHOLD_MINUTES)).isoformat()
    failed_items = scan_failed_proposals(dynamodb, retry_cutoff)
    logger.info(f"Found {len(failed_items)} failed proposals to retry")
    retried = 0
    for item in failed_items:
        if retry_proposal(dynamodb, sns, item):
            retried += 1

    return {
        "statusCode": 200,
        "body": f"Processed {len(stale_items)} stale, retried {retried}/{len(failed_items)} proposals",
    }
