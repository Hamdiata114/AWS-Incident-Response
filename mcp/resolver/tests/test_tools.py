"""Tests for mcp/resolver tools: iam_baseline and concurrency."""

import json

import boto3
import pytest
from moto import mock_aws

from config.baseline import (
    CLOUDWATCH_STATEMENT,
    FULL_POLICY_DOCUMENT,
    POLICY_NAME,
    ROLE_NAME,
    S3_STATEMENT,
)


LAMBDA_NAME = "data-processor"

TRUST_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
})


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


# ── IAM baseline fixtures ─────────────────────────────────────────────

@pytest.fixture
def iam_setup(monkeypatch):
    """Create mocked IAM role, monkeypatch module client."""
    with mock_aws():
        iam = boto3.client("iam", region_name="ca-central-1")
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=TRUST_POLICY)

        import tools.iam_baseline as mod
        monkeypatch.setattr(mod, "iam_client", iam)

        yield iam


# ── Concurrency fixtures ──────────────────────────────────────────────

@pytest.fixture
def lambda_setup(monkeypatch):
    """Create mocked Lambda function, monkeypatch module client."""
    with mock_aws():
        iam = boto3.client("iam", region_name="ca-central-1")
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=TRUST_POLICY)
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]

        lam = boto3.client("lambda", region_name="ca-central-1")
        lam.create_function(
            FunctionName=LAMBDA_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.handler",
            Code={"ZipFile": b"fake"},
        )

        import tools.concurrency as mod
        monkeypatch.setattr(mod, "lambda_client", lam)

        yield lam


# ── get_baseline_iam tests ────────────────────────────────────────────

class TestGetBaselineIAM:
    @pytest.mark.asyncio
    async def test_no_drift(self, iam_setup):
        from tools.iam_baseline import get_baseline_iam

        iam_setup.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(FULL_POLICY_DOCUMENT),
        )
        result = await get_baseline_iam(ROLE_NAME)
        assert result["drift"] is False
        assert result["current_policy"] == FULL_POLICY_DOCUMENT

    @pytest.mark.asyncio
    async def test_missing_s3_statement(self, iam_setup):
        from tools.iam_baseline import get_baseline_iam

        partial = {"Version": "2012-10-17", "Statement": [CLOUDWATCH_STATEMENT]}
        iam_setup.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(partial),
        )
        result = await get_baseline_iam(ROLE_NAME)
        assert result["drift"] is True
        sids = [s["Sid"] for s in result["current_policy"]["Statement"]]
        assert "S3Access" not in sids

    @pytest.mark.asyncio
    async def test_missing_cw_statement(self, iam_setup):
        from tools.iam_baseline import get_baseline_iam

        partial = {"Version": "2012-10-17", "Statement": [S3_STATEMENT]}
        iam_setup.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(partial),
        )
        result = await get_baseline_iam(ROLE_NAME)
        assert result["drift"] is True
        sids = [s["Sid"] for s in result["current_policy"]["Statement"]]
        assert "CloudWatchLogsAccess" not in sids

    @pytest.mark.asyncio
    async def test_both_statements_replaced(self, iam_setup):
        from tools.iam_baseline import get_baseline_iam

        # Policy exists but has neither S3 nor CloudWatch statements
        wrong = {
            "Version": "2012-10-17",
            "Statement": [{"Sid": "Other", "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"}],
        }
        iam_setup.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(wrong),
        )
        result = await get_baseline_iam(ROLE_NAME)
        assert result["drift"] is True
        sids = [s["Sid"] for s in result["current_policy"]["Statement"]]
        assert "S3Access" not in sids
        assert "CloudWatchLogsAccess" not in sids

    @pytest.mark.asyncio
    async def test_policy_detached(self, iam_setup):
        from tools.iam_baseline import get_baseline_iam

        result = await get_baseline_iam(ROLE_NAME)
        assert result["drift"] is True
        assert result["current_policy"] is None


# ── get_current_concurrency tests ─────────────────────────────────────

class TestGetCurrentConcurrency:
    @pytest.mark.asyncio
    async def test_throttled_zero(self, lambda_setup):
        from tools.concurrency import get_current_concurrency

        lambda_setup.put_function_concurrency(
            FunctionName=LAMBDA_NAME,
            ReservedConcurrentExecutions=0,
        )
        result = await get_current_concurrency(LAMBDA_NAME)
        assert result["reserved_concurrency"] == 0
        assert result["is_throttled"] is True

    @pytest.mark.asyncio
    async def test_throttled_one(self, lambda_setup):
        from tools.concurrency import get_current_concurrency

        lambda_setup.put_function_concurrency(
            FunctionName=LAMBDA_NAME,
            ReservedConcurrentExecutions=1,
        )
        result = await get_current_concurrency(LAMBDA_NAME)
        assert result["reserved_concurrency"] == 1
        assert result["is_throttled"] is True

    @pytest.mark.asyncio
    async def test_healthy_no_reservation(self, lambda_setup):
        from tools.concurrency import get_current_concurrency

        result = await get_current_concurrency(LAMBDA_NAME)
        assert result["reserved_concurrency"] is None
        assert result["is_throttled"] is False
