"""Known-good IAM baseline constants for the data-processor Lambda."""

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
        "arn:aws:s3:::lab-security-evidence-1/*",
    ],
}

CLOUDWATCH_STATEMENT = {
    "Sid": "CloudWatchLogsAccess",
    "Effect": "Allow",
    "Action": ["logs:DescribeLogStreams"],
    "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:/aws/lambda/agent-trigger-message:*",
}

FULL_POLICY_DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [S3_STATEMENT, CLOUDWATCH_STATEMENT],
}
