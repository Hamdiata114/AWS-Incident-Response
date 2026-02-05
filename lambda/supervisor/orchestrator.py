"""
Supervisor Agent Lambda - Entry point for incident response.

Receives SNS notifications when data-processor encounters errors and
initiates the incident response workflow.
"""

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Lambda handler triggered by SNS notifications from data-processor.

    Parses the SNS event, extracts incident details, and logs them.
    This is the MVP implementation - future versions will orchestrate
    the Resolution and Critic agents.

    Args:
        event: SNS event containing incident details.
        context: Lambda context object.

    Returns:
        dict: Response with statusCode and processing result.
    """
    logger.info("Supervisor agent triggered")
    logger.info(f"Received event: {json.dumps(event)}")

    incidents_processed = 0

    for record in event.get("Records", []):
        if record.get("EventSource") != "aws:sns":
            logger.warning(f"Unexpected event source: {record.get('EventSource')}")
            continue

        sns_message = record.get("Sns", {})
        subject = sns_message.get("Subject", "No subject")
        message_body = sns_message.get("Message", "{}")

        try:
            incident = json.loads(message_body)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse SNS message: {e}")
            logger.error(f"Raw message: {message_body}")
            continue

        logger.info("=" * 50)
        logger.info("INCIDENT RECEIVED")
        logger.info("=" * 50)
        logger.info(f"Subject: {subject}")
        logger.info(f"Error Type: {incident.get('error_type', 'Unknown')}")
        logger.info(f"Error Code: {incident.get('error_code', 'Unknown')}")
        logger.info(f"Error Message: {incident.get('error_message', 'No message')}")
        logger.info(f"Lambda Name: {incident.get('lambda_name', 'Unknown')}")
        logger.info(f"Timestamp: {incident.get('timestamp', 'Unknown')}")
        logger.info("=" * 50)

        incidents_processed += 1

    logger.info(f"Processed {incidents_processed} incident(s)")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Incidents processed",
            "count": incidents_processed,
        }),
    }
