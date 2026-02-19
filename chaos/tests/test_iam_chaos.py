"""Tests for chaos/iam_chaos.py."""

import json

import boto3
import pytest
from moto import mock_aws


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def iam_setup():
    """Create mocked IAM role and return client."""
    with mock_aws():
        client = boto3.client("iam")
        client.create_role(
            RoleName="lab-lambda-baisc-role",
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
            }),
        )
        yield client


# ── get_current_policy ───────────────────────────────────────────────

class TestGetCurrentPolicy:
    def test_exists(self, iam_setup):
        from iam_chaos import get_current_policy, ROLE_NAME, POLICY_NAME

        policy_doc = {"Version": "2012-10-17", "Statement": [{"Sid": "S3Access", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "*"}]}
        iam_setup.put_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy_doc))

        result = get_current_policy(iam_setup)
        assert result is not None
        assert result["Statement"][0]["Sid"] == "S3Access"

    def test_missing(self, iam_setup):
        from iam_chaos import get_current_policy

        result = get_current_policy(iam_setup)
        assert result is None


# ── put_policy ───────────────────────────────────────────────────────

class TestPutPolicy:
    def test_with_statements(self, iam_setup):
        from iam_chaos import put_policy, get_current_policy, S3_STATEMENT

        put_policy(iam_setup, [S3_STATEMENT])
        policy = get_current_policy(iam_setup)
        assert len(policy["Statement"]) == 1
        assert policy["Statement"][0]["Sid"] == "S3Access"

    def test_empty_deletes(self, iam_setup):
        from iam_chaos import put_policy, get_current_policy, S3_STATEMENT

        put_policy(iam_setup, [S3_STATEMENT])
        put_policy(iam_setup, [])
        assert get_current_policy(iam_setup) is None

    def test_empty_no_op_when_nonexistent(self, iam_setup):
        from iam_chaos import put_policy, get_current_policy

        put_policy(iam_setup, [])  # should not raise
        assert get_current_policy(iam_setup) is None


# ── revoke ───────────────────────────────────────────────────────────

class TestRevoke:
    def _setup_full_policy(self, iam_setup):
        from iam_chaos import put_policy, S3_STATEMENT, CLOUDWATCH_STATEMENT
        put_policy(iam_setup, [S3_STATEMENT, CLOUDWATCH_STATEMENT])

    def test_revoke_s3(self, iam_setup, monkeypatch):
        import iam_chaos
        monkeypatch.setattr(iam_chaos, "get_iam_client", lambda: iam_setup)
        self._setup_full_policy(iam_setup)

        iam_chaos.revoke("s3")
        policy = iam_chaos.get_current_policy(iam_setup)
        sids = [s["Sid"] for s in policy["Statement"]]
        assert "S3Access" not in sids
        assert "CloudWatchLogsAccess" in sids

    def test_revoke_cloudwatch(self, iam_setup, monkeypatch):
        import iam_chaos
        monkeypatch.setattr(iam_chaos, "get_iam_client", lambda: iam_setup)
        self._setup_full_policy(iam_setup)

        iam_chaos.revoke("cloudwatch")
        policy = iam_chaos.get_current_policy(iam_setup)
        sids = [s["Sid"] for s in policy["Statement"]]
        assert "CloudWatchLogsAccess" not in sids
        assert "S3Access" in sids

    def test_revoke_both(self, iam_setup, monkeypatch):
        import iam_chaos
        monkeypatch.setattr(iam_chaos, "get_iam_client", lambda: iam_setup)
        self._setup_full_policy(iam_setup)

        iam_chaos.revoke("both")
        assert iam_chaos.get_current_policy(iam_setup) is None

    def test_revoke_invalid(self, iam_setup, monkeypatch):
        import iam_chaos
        monkeypatch.setattr(iam_chaos, "get_iam_client", lambda: iam_setup)

        with pytest.raises(ValueError, match="Invalid target"):
            iam_chaos.revoke("invalid")


# ── restore ──────────────────────────────────────────────────────────

class TestRestore:
    def test_restore(self, iam_setup, monkeypatch):
        import iam_chaos
        monkeypatch.setattr(iam_chaos, "get_iam_client", lambda: iam_setup)

        iam_chaos.restore()
        policy = iam_chaos.get_current_policy(iam_setup)
        sids = [s["Sid"] for s in policy["Statement"]]
        assert "S3Access" in sids
        assert "CloudWatchLogsAccess" in sids


# ── get_permission_status ────────────────────────────────────────────

class TestGetPermissionStatus:
    def test_all_granted(self, iam_setup):
        from iam_chaos import get_permission_status, put_policy, S3_STATEMENT, CLOUDWATCH_STATEMENT

        put_policy(iam_setup, [S3_STATEMENT, CLOUDWATCH_STATEMENT])
        result = get_permission_status(iam_setup)
        assert result["policy_attached"] is True
        assert result["s3"] == "GRANTED"
        assert result["cloudwatch"] == "GRANTED"

    def test_no_policy(self, iam_setup):
        from iam_chaos import get_permission_status

        result = get_permission_status(iam_setup)
        assert result["policy_attached"] is False
        assert result["s3"] == "REVOKED"
        assert result["cloudwatch"] == "REVOKED"
