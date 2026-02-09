"""
Stale Incident Watchdog — EventBridge-triggered Lambda (every 5 min).

Scans incident-state for INVESTIGATING incidents with updated_at older than
10 minutes and transitions them to FAILED.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.client("dynamodb", region_name="ca-central-1")

STALE_THRESHOLD_MINUTES = 10


def handler(event, context):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_THRESHOLD_MINUTES)).isoformat()

    resp = dynamodb.scan(
        TableName="incident-state",
        FilterExpression="#s = :investigating AND updated_at < :cutoff",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":investigating": {"S": "INVESTIGATING"},
            ":cutoff": {"S": cutoff},
        },
    )

    stale_items = resp.get("Items", [])
    logger.info(f"Found {len(stale_items)} stale incidents")

    for item in stale_items:
        incident_id = item["incident_id"]["S"]
        now = datetime.now(timezone.utc).isoformat()
        try:
            dynamodb.update_item(
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
        except dynamodb.exceptions.ConditionalCheckFailedException:
            logger.info(f"Incident already transitioned, skipping: {incident_id}")

    return {"statusCode": 200, "body": f"Processed {len(stale_items)} stale incidents"}
