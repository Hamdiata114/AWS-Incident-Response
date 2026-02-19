"""Tests for mcp/supervisor/tools/lambda_config.py."""

import json

import boto3
import pytest
from moto import mock_aws


ROLE_NAME = "lab-lambda-baisc-role"

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
    """Create mocked Lambda + IAM role, monkeypatch module client."""
    with mock_aws():
        iam = boto3.client("iam", region_name="ca-central-1")
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=TRUST_POLICY)
        role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]

        lam = boto3.client("lambda", region_name="ca-central-1")
        lam.create_function(
            FunctionName="data-processor",
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.handler",
            Code={"ZipFile": b"fake"},
            MemorySize=256,
            Timeout=30,
            Environment={"Variables": {"SECRET": "do-not-leak"}},
        )

        import tools.lambda_config as mod
        monkeypatch.setattr(mod, "lambda_client", lam)

        yield lam


class TestGetLambdaConfig:
    @pytest.mark.asyncio
    async def test_filters_to_keep_fields(self, aws_setup):
        from tools.lambda_config import get_lambda_config, KEEP_FIELDS

        result = await get_lambda_config("data-processor")
        for key in result:
            assert key in KEEP_FIELDS

    @pytest.mark.asyncio
    async def test_excludes_env_vars(self, aws_setup):
        from tools.lambda_config import get_lambda_config

        result = await get_lambda_config("data-processor")
        assert "Environment" not in result

    @pytest.mark.asyncio
    async def test_handles_missing_optional_fields(self, aws_setup):
        from tools.lambda_config import get_lambda_config

        result = await get_lambda_config("data-processor")
        # ReservedConcurrentExecutions is not set, should be absent
        assert "ReservedConcurrentExecutions" not in result
        # But FunctionName should be present
        assert result["FunctionName"] == "data-processor"
