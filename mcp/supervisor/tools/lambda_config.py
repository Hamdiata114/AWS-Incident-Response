import boto3

lambda_client = boto3.client("lambda", region_name="ca-central-1")

KEEP_FIELDS = [
    "FunctionName", "Runtime", "Handler", "Role", "MemorySize",
    "Timeout", "LastModified", "State", "ReservedConcurrentExecutions",
]


async def get_lambda_config(lambda_name: str) -> dict:
    """Get Lambda function configuration metadata."""
    config = lambda_client.get_function_configuration(FunctionName=lambda_name)

    # Return subset only — strip Environment.Variables for security
    result = {}
    for field in KEEP_FIELDS:
        if field in config:
            result[field] = config[field]

    return result
