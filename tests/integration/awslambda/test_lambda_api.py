# ALL TESTS IN HERE ARE VALIDATED AGAINST AWS CLOUD
import json
import logging
import os.path

import pytest

from localstack.utils.functions import run_safe
from localstack.utils.strings import short_uid
from localstack.utils.sync import retry, wait_until

LOG = logging.Logger(__name__)

role_assume_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

role_policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": ["*"],
        }
    ],
}


@pytest.fixture
def create_lambda_function_aws(
    lambda_client,
    iam_client,
):
    lambda_arns = []
    iam_role_names = []

    def _create_lambda_function(**kwargs):
        kwargs["client"] = lambda_client

        if not kwargs.get("role"):
            role_name = f"lambda-autogenerated-{short_uid()}"
            iam_role_names.append(role_name)
            doc = json.dumps(role_assume_policy)
            role = iam_client.create_role(RoleName=role_name, AssumeRolePolicyDocument=doc)["Role"]
            policy_name = f"lambda-autogenerated-{short_uid()}"
            policy_arn = iam_client.create_policy(
                PolicyName=policy_name, PolicyDocument=json.dumps(role_policy)
            )["Policy"]["Arn"]
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            kwargs["role"] = role["Arn"]

        def _create_function():
            resp = lambda_client.create_function(**kwargs)
            lambda_arns.append(resp["FunctionArn"])

            def _is_not_pending():
                try:
                    result = (
                        lambda_client.get_function(FunctionName=kwargs["func_name"])[
                            "Configuration"
                        ]["State"]
                        != "Pending"
                    )
                    LOG.debug(f"lambda state result: {result=}")
                    return result
                except Exception as e:
                    LOG.error(e)
                    raise

            wait_until(_is_not_pending)
            return resp

        # @AWS, takes about 10s until the role/policy is "active", until then it will fail
        # localstack should normally not require the retries and will just continue here
        return retry(_create_function, retries=3, sleep=4)

    yield _create_lambda_function

    for arn in lambda_arns:
        try:
            lambda_client.delete_function(FunctionName=arn)
        except Exception:
            LOG.debug(f"Unable to delete function {arn=} in cleanup")

    for role_name in iam_role_names:
        try:
            iam_client.delete_role(RoleName=role_name)
        except Exception:
            LOG.debug(f"Unable to delete role {role_name=} in cleanup")


code = """
def handler(event,ctx):
    print("hyello world!")
"""


class TestLambdaAsfApi:
    def test_create_function(self, lambda_client, create_lambda_function_aws, lambda_su_role):
        fn_name = f"ls-fn-{short_uid()}"
        with open(os.path.join(os.path.dirname(__file__), "functions/echo.zip"), "rb") as f:
            txt = f.read()
            create_result = lambda_client.create_function(
                FunctionName=fn_name,
                Handler="index.handler",
                Code={"ZipFile": txt},
                PackageType="Zip",
                Role=lambda_su_role,
                Runtime="python3.9",
            )
            try:
                assert 201 == create_result["ResponseMetadata"]["HTTPStatusCode"]
                assert create_result["ResponseMetadata"]["RequestId"]
                assert create_result["Role"] == lambda_su_role
                assert create_result["Handler"] == "index.handler"
                assert create_result["PackageType"] == "Zip"

                # calculated properties
                assert create_result["CodeSize"] == 276
                assert create_result["CodeSha256"] == "zMYxuJ0J/jyyHt1fYZUuOqZ/Gc9Gm64Wp8fT6XNiXro="

                # created properties
                assert fn_name in create_result["FunctionArn"]
                assert create_result["LastModified"]
                assert create_result["RevisionId"]

                # defaults
                assert create_result["Timeout"] == 3
                assert create_result["Description"] == ""
                assert create_result["Version"] == "$LATEST"
                assert create_result["Architectures"] == ["x86_64"]
                assert create_result["MemorySize"] == 128
                assert create_result["TracingConfig"] == {"Mode": "PassThrough"}

                # state (this might be flaky)
                assert create_result["State"] == "Pending"
                assert create_result["StateReason"] == "The function is being created."
                assert create_result["StateReasonCode"] == "Creating"
                assert create_result["LastUpdateStatus"] == "Creating"
                assert create_result["LastUpdateStatusReason"] == "Creating"
                assert create_result["LastUpdateStatusReasonCode"] == "Creating"

                get_function_result = lambda_client.get_function(FunctionName=fn_name)
                assert 200 == get_function_result["ResponseMetadata"]["HTTPStatusCode"]
                assert get_function_result["ResponseMetadata"]["RequestId"]
            finally:
                run_safe(lambda_client.delete_function(FunctionName=fn_name))
