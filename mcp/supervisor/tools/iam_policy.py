import boto3

lambda_client = boto3.client("lambda", region_name="ca-central-1")
iam_client = boto3.client("iam")

SUPPORTED_LAMBDAS = {"data-processor"}


def validate_lambda_name(lambda_name: str) -> dict | None:
    """Return error dict if unsupported, else None."""
    if lambda_name not in SUPPORTED_LAMBDAS:
        return {"error": f"Unsupported lambda: {lambda_name}. Supported: {SUPPORTED_LAMBDAS}"}
    return None


def get_role_from_lambda(lambda_name: str) -> str:
    """Return the IAM role name for a Lambda function."""
    fn = lambda_client.get_function(FunctionName=lambda_name)
    role_arn = fn["Configuration"]["Role"]
    return role_arn.split("/")[-1]


def get_attached_policies(role_name: str) -> list[str]:
    """Return list of managed policy ARNs attached to the role."""
    attached = iam_client.list_attached_role_policies(RoleName=role_name)
    return [p["PolicyArn"] for p in attached["AttachedPolicies"]]


def get_inline_policies(role_name: str) -> dict:
    """Return {policy_name: policy_document} for all inline policies."""
    inline_names = iam_client.list_role_policies(RoleName=role_name)["PolicyNames"]
    result = {}
    for name in inline_names:
        doc = iam_client.get_role_policy(RoleName=role_name, PolicyName=name)
        result[name] = doc["PolicyDocument"]
    return result


async def get_iam_state(lambda_name: str) -> dict:
    """Get current IAM policy state for a Lambda's execution role."""
    err = validate_lambda_name(lambda_name)
    if err:
        return err

    role_name = get_role_from_lambda(lambda_name)

    return {
        "role_name": role_name,
        "inline_policies": get_inline_policies(role_name),
        "attached_policies": get_attached_policies(role_name),
    }
