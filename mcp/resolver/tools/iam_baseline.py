"""Tool: compare current IAM inline policy against known-good baseline."""

import boto3
from botocore.exceptions import ClientError

from config.baseline import FULL_POLICY_DOCUMENT, POLICY_NAME

iam_client = boto3.client("iam")


async def get_baseline_iam(role_name: str) -> dict:
    """Compare current inline policy vs baseline, return drift info."""
    try:
        resp = iam_client.get_role_policy(
            RoleName=role_name,
            PolicyName=POLICY_NAME,
        )
        current_policy = resp["PolicyDocument"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            current_policy = None
        else:
            return {"error": str(e)}

    drift = current_policy != FULL_POLICY_DOCUMENT

    return {
        "role_name": role_name,
        "policy_name": POLICY_NAME,
        "expected_policy": FULL_POLICY_DOCUMENT,
        "current_policy": current_policy,
        "drift": drift,
    }
