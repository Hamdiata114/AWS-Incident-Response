"""
Data Processor Lambda - Target for chaos engineering experiments.

Validates access to S3 and CloudWatch resources. Prints "Processing" on success;
raises an error on failure.
"""

import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = "lab-security-evidence-1"
CLOUDWATCH_LOG_GROUP = "/aws/lambda/agent-trigger-message"
SNS_TOPIC_ARN = "arn:aws:sns:ca-central-1:534321188934:incident-alerts"
LAMBDA_NAME = "data-processor"


class S3AccessError(Exception):
    """Raised when S3 bucket access fails."""

    def __init__(self, message: str, original_error: Exception = None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


class CloudWatchAccessError(Exception):
    """Raised when CloudWatch log group access fails."""

    def __init__(self, message: str, original_error: Exception = None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


def publish_incident(error_type: str, error_message: str, error_code: str = "Unknown") -> None:
    """
    Publish incident details to SNS topic for the supervisor agent.

    Args:
        error_type: The type/class of the error (e.g., "S3AccessError").
        error_message: Detailed error message.
        error_code: AWS error code if available.
    """
    try:
        sns_client = boto3.client("sns", region_name="ca-central-1")
        message = {
            "error_type": error_type,
            "error_message": error_message,
            "error_code": error_code,
            "lambda_name": LAMBDA_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps(message),
            Subject=f"Incident: {error_type} in {LAMBDA_NAME}",
        )
        logger.info(f"Published incident to SNS: {error_type}")
    except Exception as e:
        logger.error(f"Failed to publish incident to SNS: {e}")


def check_s3_access(s3_client, bucket_name: str) -> None:
    """
    Verify access to the S3 bucket using list_objects_v2.

    Raises:
        S3AccessError: If access check fails.
    """
    try:
        logger.info(f"Checking S3 access for bucket: {bucket_name}")
        s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=1)
        logger.info("S3 access check passed")
    except (ClientError, BotoCoreError) as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "Unknown")
        error_msg = str(e)
        logger.error(f"S3 access failed: {error_code} - {error_msg}")
        full_message = f"Failed to access S3 bucket '{bucket_name}': {error_code} - {error_msg}"
        publish_incident("S3AccessError", full_message, error_code)
        raise S3AccessError(full_message, original_error=e)


def check_cloudwatch_access(logs_client, log_group_name: str) -> None:
    """
    Verify access to the CloudWatch log group using describe_log_streams.

    Raises:
        CloudWatchAccessError: If access check fails.
    """
    try:
        logger.info(f"Checking CloudWatch access for log group: {log_group_name}")
        logs_client.describe_log_streams(logGroupName=log_group_name, limit=1)
        logger.info("CloudWatch access check passed")
    except (ClientError, BotoCoreError) as e:
        error_code = getattr(e, "response", {}).get("Error", {}).get("Code", "Unknown")
        error_msg = str(e)
        logger.error(f"CloudWatch access failed: {error_code} - {error_msg}")
        full_message = f"Failed to access CloudWatch log group '{log_group_name}': {error_code} - {error_msg}"
        publish_incident("CloudWatchAccessError", full_message, error_code)
        raise CloudWatchAccessError(full_message, original_error=e)


def handler(event, context):
    """
    Lambda handler that validates S3 and CloudWatch access.

    Returns:
        dict: Success response with statusCode 200.

    Raises:
        S3AccessError: If S3 access fails.
        CloudWatchAccessError: If CloudWatch access fails.
    """
    logger.info("Starting data processor")

    s3_client = boto3.client("s3")
    logs_client = boto3.client("logs")

    check_s3_access(s3_client, S3_BUCKET)
    check_cloudwatch_access(logs_client, CLOUDWATCH_LOG_GROUP)

    print("Processing")
    logger.info("All access checks passed")

    return {"statusCode": 200, "body": "Processing complete"}
