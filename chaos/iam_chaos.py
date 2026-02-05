#!/usr/bin/env python3
"""
IAM Chaos Script - Revokes and restores IAM permissions for the data-processor Lambda.

Usage:
    python iam_chaos.py revoke --target s3|cloudwatch|both
    python iam_chaos.py restore
    python iam_chaos.py status
"""

import argparse
import json
import boto3
from botocore.exceptions import ClientError

# Configuration
ROLE_NAME = "lab-lambda-baisc-role"
POLICY_NAME = "data-processor-access"
ACCOUNT_ID = "534321188934"
REGION = "ca-central-1"

S3_STATEMENT = {
    "Sid": "S3Access",
    "Effect": "Allow",
    "Action": ["s3:ListBucket", "s3:GetObject"],
    "Resource": [
        "arn:aws:s3:::lab-security-evidence-1",
        "arn:aws:s3:::lab-security-evidence-1/*"
    ]
}

CLOUDWATCH_STATEMENT = {
    "Sid": "CloudWatchLogsAccess",
    "Effect": "Allow",
    "Action": ["logs:DescribeLogStreams"],
    "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/lambda/agent-trigger-message:*"
}


def get_iam_client():
    return boto3.client("iam")


def get_current_policy(iam_client):
    """Get the current policy document, or None if it doesn't exist."""
    try:
        response = iam_client.get_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME
        )
        return response["PolicyDocument"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchEntity":
            return None
        raise


def put_policy(iam_client, statements):
    """Update the role policy with the given statements."""
    if not statements:
        # No statements = delete the policy entirely
        try:
            iam_client.delete_role_policy(
                RoleName=ROLE_NAME,
                PolicyName=POLICY_NAME
            )
            print(f"Policy '{POLICY_NAME}' deleted from role '{ROLE_NAME}'")
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                raise
    else:
        policy_document = {
            "Version": "2012-10-17",
            "Statement": statements
        }
        iam_client.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(policy_document)
        )
        print(f"Policy '{POLICY_NAME}' updated on role '{ROLE_NAME}'")


def revoke(target: str):
    """
    Revoke permissions based on target.

    Args:
        target: 's3', 'cloudwatch', or 'both'
    """
    iam_client = get_iam_client()

    if target == "both":
        # Revoke all - delete the policy
        put_policy(iam_client, [])
        print("Revoked: S3 and CloudWatch permissions")
    elif target == "s3":
        # Keep only CloudWatch
        put_policy(iam_client, [CLOUDWATCH_STATEMENT])
        print("Revoked: S3 permissions (CloudWatch retained)")
    elif target == "cloudwatch":
        # Keep only S3
        put_policy(iam_client, [S3_STATEMENT])
        print("Revoked: CloudWatch permissions (S3 retained)")
    else:
        raise ValueError(f"Invalid target: {target}. Must be 's3', 'cloudwatch', or 'both'")


def restore():
    """Restore all permissions."""
    iam_client = get_iam_client()
    put_policy(iam_client, [S3_STATEMENT, CLOUDWATCH_STATEMENT])
    print("Restored: S3 and CloudWatch permissions")


def status():
    """Show current permission status."""
    iam_client = get_iam_client()
    policy = get_current_policy(iam_client)

    if policy is None:
        print("Status: No policy attached")
        print("  S3:         REVOKED")
        print("  CloudWatch: REVOKED")
        return

    statements = policy.get("Statement", [])
    sids = [s.get("Sid") for s in statements]

    s3_status = "GRANTED" if "S3Access" in sids else "REVOKED"
    cw_status = "GRANTED" if "CloudWatchLogsAccess" in sids else "REVOKED"

    print(f"Status: Policy '{POLICY_NAME}' attached")
    print(f"  S3:         {s3_status}")
    print(f"  CloudWatch: {cw_status}")


def main():
    parser = argparse.ArgumentParser(
        description="IAM Chaos Script - Revoke/restore Lambda permissions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Revoke command
    revoke_parser = subparsers.add_parser("revoke", help="Revoke permissions")
    revoke_parser.add_argument(
        "--target",
        choices=["s3", "cloudwatch", "both"],
        required=True,
        help="Which permissions to revoke"
    )

    # Restore command
    subparsers.add_parser("restore", help="Restore all permissions")

    # Status command
    subparsers.add_parser("status", help="Show current permission status")

    args = parser.parse_args()

    if args.command == "revoke":
        revoke(args.target)
    elif args.command == "restore":
        restore()
    elif args.command == "status":
        status()


if __name__ == "__main__":
    main()
