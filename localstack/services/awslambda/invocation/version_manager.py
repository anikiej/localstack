import dataclasses
import logging
import queue
import threading
import uuid
from concurrent.futures import Future
from datetime import datetime
from queue import Queue
from threading import Thread
from typing import TYPE_CHECKING, Dict, List, Optional

from localstack.aws.api.awslambda import State
from localstack.services.awslambda.invocation.executor_endpoint import (
    ExecutorEndpoint,
    InvocationError,
    InvocationLogs,
    InvocationResult,
    ServiceEndpoint,
)
from localstack.services.awslambda.invocation.runtime_executor import (
    cleanup_version,
    prepare_version,
)
from localstack.services.awslambda.invocation.runtime_handler import (
    InvalidStatusException,
    RuntimeEnvironment,
    RuntimeStatus,
)
from localstack.utils.cloudwatch.cloudwatch_util import store_cloudwatch_logs
from localstack.utils.net import get_free_tcp_port

if TYPE_CHECKING:
    from localstack.services.awslambda.invocation.lambda_service import FunctionVersion, Invocation

LOG = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class InvocationStorage:
    invocation_id: str
    result_future: Future
    retries: int
    invocation: "Invocation"


@dataclasses.dataclass
class RunningInvocation:
    invocation: InvocationStorage
    start_time: datetime
    executor: RuntimeEnvironment
    logs: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class LogItem:
    log_group: str
    log_stream: str
    logs: str


class LogHandler:
    log_queue: Queue
    _thread: Optional[Thread]
    _shutdown_event: threading.Event

    def __init__(self):
        self.log_queue = Queue()
        self._shutdown_event = threading.Event()
        self._thread = None

    def run_log_loop(self):
        while not self._shutdown_event.is_set():
            try:
                log_item = self.log_queue.get(timeout=1)
            except queue.Empty:
                continue
            store_cloudwatch_logs(log_item.log_group, log_item.log_stream, log_item.logs)

    def start_subscriber(self):
        self._thread = Thread(target=self.run_log_loop)
        self._thread.start()

    def add_logs(self, log_item: LogItem):
        self.log_queue.put(log_item)

    def stop(self):
        self._shutdown_event.set()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            LOG.error("Could not stop log subscriber in time")


class LambdaVersionManager(ServiceEndpoint):
    # arn this Lambda Version manager manages
    function_arn: str
    function_version: "FunctionVersion"
    # mapping from invocation id to invocation storage
    running_invocations: Dict[str, RunningInvocation]
    # stack of available environments
    available_environments: queue.LifoQueue
    # mapping environment id -> environment
    all_environments: Dict[str, RuntimeEnvironment]
    queued_invocations: Queue
    provisioned_concurrent_executions: int
    executor_endpoint: Optional[ExecutorEndpoint]
    invocation_thread: Optional[Thread]
    shutdown_event: threading.Event
    state: str
    log_handler: LogHandler

    def __init__(
        self,
        function_arn: str,
        function_version: "FunctionVersion",
    ):
        self.function_arn = function_arn
        self.function_version = function_version
        self.running_invocations = {}
        self.available_environments = queue.LifoQueue()
        self.all_environments = {}
        self.queued_invocations = Queue()
        self.provisioned_concurrent_executions = 0
        self.executor_endpoint = None
        self.invocation_thread = None
        self.shutdown_event = threading.Event()
        self.state = State.Inactive
        self.log_handler = LogHandler()

    def _build_executor_endpoint(self) -> ExecutorEndpoint:
        port = get_free_tcp_port()
        executor_endpoint = ExecutorEndpoint(port, service_endpoint=self)
        executor_endpoint.start()
        return executor_endpoint

    def start(self) -> None:
        self.executor_endpoint = self._build_executor_endpoint()
        invocation_thread = Thread(target=self.invocation_loop)
        invocation_thread.start()
        self.invocation_thread = invocation_thread
        self.log_handler.start_subscriber()

        prepare_version(self.function_version)
        LOG.debug(f"Lambda '{self.function_arn}' changed to active")
        self.state = State.Active
        LOG.debug("Init complete")

    def stop(self) -> None:
        LOG.debug("Stopping lambda version '%s'", self.function_arn)
        cleanup_version(self.function_version)
        self.shutdown_event.set()
        try:
            self.invocation_thread.join(timeout=5.0)
            LOG.debug("Thread stopped '%s'", self.function_arn)
        except TimeoutError:
            LOG.debug("Thread did not stop after 5s '%s'", self.function_arn)

        try:
            self.executor_endpoint.shutdown()
        except Exception as e:
            LOG.debug(
                "Error while stopping executor endpoint for lambda %s, error: %s",
                self.function_arn,
                e,
            )
        self.log_handler.stop()
        for environment in list(self.all_environments.values()):
            self.stop_environment(environment)

    def update_provisioned_concurrency_config(self, provisioned_concurrent_executions: int) -> None:
        self.provisioned_concurrent_executions = provisioned_concurrent_executions
        # TODO initialize/destroy runners if applicable

    def start_environment(self) -> RuntimeEnvironment:
        LOG.debug("Starting new environment")
        runtime_environment = RuntimeEnvironment(
            function_version=self.function_version,
            executor_endpoint=self.executor_endpoint,
            initialization_type="on-demand",
        )
        self.all_environments[runtime_environment.id] = runtime_environment
        # TODO async?
        runtime_environment.start()

        return runtime_environment

    def stop_environment(self, environment: RuntimeEnvironment):
        try:
            environment.stop()
            self.all_environments.pop(environment.id)
        except Exception as e:
            LOG.debug(
                "Error while stopping environment for lambda %s, environment: %s, error: %s",
                self.function_arn,
                environment.id,
                e,
            )

    def count_environment_by_status(self, status: List[RuntimeStatus]):
        return len(
            [runtime for runtime in self.all_environments.values() if runtime.status in status]
        )

    def ready_environment_count(self) -> int:
        return self.count_environment_by_status([RuntimeStatus.READY])

    def active_environment_count(self):
        return self.count_environment_by_status(
            [RuntimeStatus.READY, RuntimeStatus.STARTING, RuntimeStatus.RUNNING]
        )

    def invocation_loop(self):
        # TODO cleanup shutdown logic
        while True:
            invocation_storage = None
            while not invocation_storage:
                try:
                    invocation_storage = self.queued_invocations.get(timeout=0.5)
                    LOG.debug("Got invocation event %s in loop", invocation_storage.invocation_id)
                except queue.Empty:
                    if self.shutdown_event.is_set():
                        LOG.debug(
                            "Invocation loop for lambda %s stopped while waiting for invocations",
                            self.function_arn,
                        )
                        return
            # TODO refine environment startup logic
            if self.available_environments.empty() or self.active_environment_count() == 0:
                self.start_environment()
            environment = None
            while not environment:
                try:
                    environment = self.available_environments.get(timeout=1)
                    self.running_invocations[invocation_storage.invocation_id] = RunningInvocation(
                        invocation_storage, datetime.now(), executor=environment
                    )
                    environment.invoke(invocation_event=invocation_storage)
                    LOG.debug("Invoke for request %s done", invocation_storage.invocation_id)
                except queue.Empty:
                    if self.active_environment_count() == 0:
                        self.start_environment()
                    # TODO what to do with too much timeouts?
                    if self.shutdown_event.is_set():
                        # TODO what to do with current event?
                        LOG.debug(
                            "Invocation loop for lambda %s stopped while waiting for environments",
                            self.function_arn,
                        )
                        return
                except InvalidStatusException:
                    LOG.debug(
                        "Retrieved environment %s in invalid state from queue. Trying the next...",
                        environment.id,
                    )
                    environment = None
                    self.running_invocations.pop(invocation_storage.invocation_id, None)
                    continue

    def invoke(self, *, invocation: "Invocation") -> Future:
        invocation_storage = InvocationStorage(
            invocation_id=str(uuid.uuid4()),
            result_future=Future(),
            retries=1,
            invocation=invocation,
        )
        self.queued_invocations.put(invocation_storage)

        return invocation_storage.result_future

    def set_environment_ready(self, executor_id: str) -> None:
        environment = self.all_environments.get(executor_id)
        if not environment:
            raise Exception(
                "Inconsistent state detected: Non existing environment '%s' reported error.",
                executor_id,
            )
        environment.set_ready()
        self.available_environments.put(environment)

    def set_environment_failed(self, executor_id: str) -> None:
        environment = self.all_environments.get(executor_id)
        if not environment:
            raise Exception(
                "Inconsistent state detected: Non existing environment '%s' reported error.",
                executor_id,
            )
        environment.errored()

    def store_logs(self, invocation_result: InvocationResult, executor: RuntimeEnvironment):
        log_item = LogItem(
            executor.get_log_group_name(), executor.get_log_stream_name(), invocation_result.logs
        )
        self.log_handler.add_logs(log_item)

    # Service Endpoint implementation
    def invocation_result(self, invoke_id: str, invocation_result: InvocationResult) -> None:
        LOG.debug("Got invocation result for invocation '%s'", invoke_id)
        running_invocation = self.running_invocations.pop(invoke_id, None)
        if running_invocation is None:
            raise Exception(f"Cannot map invocation result {invoke_id} to invocation")

        if not invocation_result.logs:
            invocation_result.logs = running_invocation.logs
        executor = running_invocation.executor
        running_invocation.invocation.result_future.set_result(invocation_result)
        # mark executor available again
        executor.invocation_done()
        self.available_environments.put(executor)
        self.store_logs(invocation_result=invocation_result, executor=executor)

    def invocation_error(self, invoke_id: str, invocation_error: InvocationError) -> None:
        LOG.error("Fucked up %s", invoke_id)

    def invocation_logs(self, invoke_id: str, invocation_logs: InvocationLogs) -> None:
        LOG.debug("Got logs for invocation '%s'", invoke_id)
        for log_line in invocation_logs.logs.splitlines():
            LOG.debug("> %s", log_line)
        running_invocation = self.running_invocations.get(invoke_id, None)
        if running_invocation is None:
            raise Exception(f"Cannot map invocation result {invoke_id} to invocation")
        running_invocation.logs = invocation_logs.logs

    def status_ready(self, executor_id: str) -> None:
        self.set_environment_ready(executor_id=executor_id)

    def status_error(self, executor_id: str) -> None:
        self.set_environment_failed(executor_id=executor_id)