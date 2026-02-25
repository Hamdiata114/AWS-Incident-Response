"""Tool: check Lambda reserved concurrency and flag throttling."""

import boto3
from botocore.exceptions import ClientError

lambda_client = boto3.client("lambda", region_name="ca-central-1")


async def get_current_concurrency(lambda_name: str) -> dict:
    """Get reserved concurrency for a Lambda, flag if throttled (0 or 1)."""
    reserved = None

    try:
        resp = lambda_client.get_function_concurrency(FunctionName=lambda_name)
        reserved = resp.get("ReservedConcurrentExecutions")
    except ClientError:
        pass

    return {
        "lambda_name": lambda_name,
        "reserved_concurrency": reserved,
        "is_throttled": reserved is not None and reserved <= 1,
    }
