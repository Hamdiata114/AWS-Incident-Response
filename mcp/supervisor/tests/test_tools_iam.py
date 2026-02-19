"""Tests for mcp/supervisor/tools/iam_policy.py."""

import json

import boto3
import pytest
from moto import mock_aws


ROLE_NAME = "lab-lambda-baisc-role"
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


@pytest.fixture
def aws_setup(monkeypatch):
    """Create mocked IAM role + Lambda function, monkeypatch module clients."""
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

        import tools.iam_policy as mod
        monkeypatch.setattr(mod, "lambda_client", lam)
        monkeypatch.setattr(mod, "iam_client", iam)

        yield {"iam": iam, "lambda": lam}


# ── validate_lambda_name ─────────────────────────────────────────────

class TestValidateLambdaName:
    def test_supported(self):
        from tools.iam_policy import validate_lambda_name
        assert validate_lambda_name("data-processor") is None

    def test_unsupported(self):
        from tools.iam_policy import validate_lambda_name
        result = validate_lambda_name("other-fn")
        assert "error" in result


# ── get_role_from_lambda ─────────────────────────────────────────────

class TestGetRoleFromLambda:
    def test_extracts_role_name(self, aws_setup):
        from tools.iam_policy import get_role_from_lambda
        assert get_role_from_lambda(LAMBDA_NAME) == ROLE_NAME


# ── get_attached_policies ────────────────────────────────────────────

class TestGetAttachedPolicies:
    def test_with_policies(self, aws_setup):
        from tools.iam_policy import get_attached_policies

        # Create and attach a managed policy (moto doesn't have AWS managed policies)
        policy = aws_setup["iam"].create_policy(
            PolicyName="TestReadOnly",
            PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"*"}]}',
        )
        arn = policy["Policy"]["Arn"]
        aws_setup["iam"].attach_role_policy(RoleName=ROLE_NAME, PolicyArn=arn)

        result = get_attached_policies(ROLE_NAME)
        assert arn in result

    def test_empty(self, aws_setup):
        from tools.iam_policy import get_attached_policies
        assert get_attached_policies(ROLE_NAME) == []


# ── get_inline_policies ─────────────────────────────────────────────

class TestGetInlinePolicies:
    def test_with_policies(self, aws_setup):
        from tools.iam_policy import get_inline_policies

        doc = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}
        aws_setup["iam"].put_role_policy(RoleName=ROLE_NAME, PolicyName="test-policy", PolicyDocument=json.dumps(doc))

        result = get_inline_policies(ROLE_NAME)
        assert "test-policy" in result

    def test_empty(self, aws_setup):
        from tools.iam_policy import get_inline_policies
        assert get_inline_policies(ROLE_NAME) == {}


# ── get_iam_state (async) ───────────────────────────────────────────

class TestGetIamState:
    @pytest.mark.asyncio
    async def test_happy_path(self, aws_setup):
        from tools.iam_policy import get_iam_state

        result = await get_iam_state(LAMBDA_NAME)
        assert result["role_name"] == ROLE_NAME
        assert "inline_policies" in result
        assert "attached_policies" in result

    @pytest.mark.asyncio
    async def test_unsupported_lambda(self, aws_setup):
        from tools.iam_policy import get_iam_state

        result = await get_iam_state("bad-lambda")
        assert "error" in result
