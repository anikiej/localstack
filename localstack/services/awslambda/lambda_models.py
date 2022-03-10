# this is trying out something by mapping the cloudformation models
import dataclasses
from typing import Any, Dict, List

# Function


@dataclasses.dataclass
class Code:
    image_uri: str
    s3_bucket: str
    s3_key: str
    s3_object_version: str
    zip_file: str


@dataclasses.dataclass
class DeadLetterConfig:
    target_arn: str


@dataclasses.dataclass
class Environment:
    variables: Dict[str, str]


@dataclasses.dataclass
class FileSystemConfig:
    arn: str
    local_mount_path: str


@dataclasses.dataclass
class ImageConfig:
    command: List[str]
    entrypoint: List[str]
    working_directory: str


@dataclasses.dataclass
class TracingConfig:
    mode: str


@dataclasses.dataclass
class VpcConfig:
    security_group_ids: List[str]
    subnet_ids: List[str]


@dataclasses.dataclass
class Function:
    function_name: str

    description: str
    role: str

    package_type: str
    architectures: List[str]
    code: Code
    layers: List[str]
    runtime: str

    timeout: int
    reserved_concurrent_executions: int
    memory_size: int
    handler: str
    image_config: ImageConfig
    environment: Environment

    # code_signing_config_arn: str
    # dead_letter_config: DeadLetterConfig
    # file_system_configs: FileSystemConfig
    # kms_key_arn: str
    # tags: List[Tag] # handled by the TAGGING service
    # tracing_config: TracingConfig
    # vpc_config: VpcConfig


# ALIAS & Version


@dataclasses.dataclass
class VersionWeight:
    function_version: str
    function_weight: float


@dataclasses.dataclass
class AliasRoutingConfiguration:
    version_weights: List[VersionWeight]


@dataclasses.dataclass
class ProvisionedConcurrencyConfiguration:
    provisioned_concurrent_executions: int


@dataclasses.dataclass
class Alias:
    function_name: str
    function_version: str
    name: str

    description: str
    routing_configuration: AliasRoutingConfiguration
    provisioned_concurrency_configuration: ProvisionedConcurrencyConfiguration


@dataclasses.dataclass
class Version:
    function_name: str
    code_sha_256: str
    description: str
    provisioned_concurrency_config: ProvisionedConcurrencyConfiguration


# ASYNC


@dataclasses.dataclass
class OnFailure:
    destination: str


@dataclasses.dataclass
class OnSuccess:
    destination: str


@dataclasses.dataclass
class DestinationConfig:
    on_failure: OnFailure
    on_success: OnSuccess


@dataclasses.dataclass
class EventInvokeConfig:
    function_name: str
    qualifier: str
    maximum_retry_attemps: int
    maximum_event_age_in_seconds: int
    destination_config: DestinationConfig


## Additions


class LastUpdate:
    status: Any
    reason: Any
    code: Any


class State:  # this is coming from the LambdaVersionManager
    state: Any
    reason: Any
    code: Any
