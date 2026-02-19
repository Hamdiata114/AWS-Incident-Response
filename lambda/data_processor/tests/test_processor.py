"""Tests for lambda/data_processor/processor.py."""

import json

import boto3
import pytest
from moto import mock_aws
from botocore.exceptions import ClientError


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ca-central-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def aws_resources():
    """Create mocked S3 bucket, SNS topic, and CloudWatch log group."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="ca-central-1")
        s3.create_bucket(
            Bucket="lab-security-evidence-1",
            CreateBucketConfiguration={"LocationConstraint": "ca-central-1"},
        )

        sns = boto3.client("sns", region_name="ca-central-1")
        sns.create_topic(Name="incident-alerts")

        logs = boto3.client("logs", region_name="ca-central-1")
        logs.create_log_group(logGroupName="/aws/lambda/agent-trigger-message")

        yield {
            "s3": boto3.client("s3", region_name="ca-central-1"),
            "logs": boto3.client("logs", region_name="ca-central-1"),
            "sns": sns,
        }


# ── Custom Exceptions ────────────────────────────────────────────────

class TestExceptions:
    def test_s3_access_error_stores_original(self):
        from processor import S3AccessError

        orig = RuntimeError("boom")
        err = S3AccessError("msg", original_error=orig)
        assert err.original_error is orig
        assert str(err) == "msg"

    def test_cloudwatch_access_error_stores_original(self):
        from processor import CloudWatchAccessError

        orig = RuntimeError("boom")
        err = CloudWatchAccessError("msg", original_error=orig)
        assert err.original_error is orig
        assert str(err) == "msg"


# ── check_s3_access ──────────────────────────────────────────────────

class TestCheckS3Access:
    def test_success(self, aws_resources):
        from processor import check_s3_access

        check_s3_access(aws_resources["s3"], "lab-security-evidence-1")  # no raise

    def test_access_denied_raises(self, aws_resources):
        from processor import check_s3_access, S3AccessError

        # Use a non-existent bucket to trigger an error
        with pytest.raises(S3AccessError, match="Failed to access S3"):
            check_s3_access(aws_resources["s3"], "nonexistent-bucket-xyz")

    def test_publishes_to_sns(self, aws_resources, monkeypatch):
        from processor import check_s3_access, S3AccessError
        import processor

        published = []
        original_publish = processor.publish_incident

        def capture_publish(*args, **kwargs):
            published.append(args)
            original_publish(*args, **kwargs)

        monkeypatch.setattr(processor, "publish_incident", capture_publish)

        with pytest.raises(S3AccessError):
            check_s3_access(aws_resources["s3"], "nonexistent-bucket-xyz")

        assert len(published) == 1
        assert published[0][0] == "S3AccessError"


# ── check_cloudwatch_access ──────────────────────────────────────────

class TestCheckCloudwatchAccess:
    def test_success(self, aws_resources):
        from processor import check_cloudwatch_access

        check_cloudwatch_access(aws_resources["logs"], "/aws/lambda/agent-trigger-message")

    def test_denied_raises(self, aws_resources):
        from processor import check_cloudwatch_access, CloudWatchAccessError

        with pytest.raises(CloudWatchAccessError, match="Failed to access CloudWatch"):
            check_cloudwatch_access(aws_resources["logs"], "/nonexistent/log-group")

    def test_publishes_to_sns(self, aws_resources, monkeypatch):
        from processor import check_cloudwatch_access, CloudWatchAccessError
        import processor

        published = []
        original_publish = processor.publish_incident

        def capture_publish(*args, **kwargs):
            published.append(args)
            original_publish(*args, **kwargs)

        monkeypatch.setattr(processor, "publish_incident", capture_publish)

        with pytest.raises(CloudWatchAccessError):
            check_cloudwatch_access(aws_resources["logs"], "/nonexistent/log-group")

        assert len(published) == 1
        assert published[0][0] == "CloudWatchAccessError"


# ── publish_incident ─────────────────────────────────────────────────

class TestPublishIncident:
    def test_success(self, aws_resources):
        from processor import publish_incident

        publish_incident("TestError", "test message", "TestCode")  # no raise

    def test_sns_failure_logs_error(self, monkeypatch):
        from processor import publish_incident
        import processor

        # Monkeypatch boto3.client to return a broken SNS client
        def broken_client(service, **kwargs):
            raise Exception("SNS down")

        monkeypatch.setattr("boto3.client", broken_client)

        # Should not raise, just log
        publish_incident("TestError", "test message")


# ── handler ──────────────────────────────────────────────────────────

class TestHandler:
    def test_success(self, aws_resources):
        from processor import handler

        resp = handler({}, None)
        assert resp["statusCode"] == 200

    def test_s3_failure_raises(self, monkeypatch):
        from processor import handler, S3AccessError

        def broken_s3(*args, **kwargs):
            raise S3AccessError("boom")

        import processor
        monkeypatch.setattr(processor, "check_s3_access", broken_s3)

        with pytest.raises(S3AccessError):
            handler({}, None)

    def test_cw_failure_raises(self, aws_resources, monkeypatch):
        from processor import handler, CloudWatchAccessError

        def broken_cw(*args, **kwargs):
            raise CloudWatchAccessError("boom")

        import processor
        monkeypatch.setattr(processor, "check_cloudwatch_access", broken_cw)

        with pytest.raises(CloudWatchAccessError):
            handler({}, None)
