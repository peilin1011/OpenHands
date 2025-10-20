import asyncio
import io
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from openhands.core.config import OpenHandsConfig
from openhands.core.exceptions import (
    AgentRuntimeDisconnectedError,
    AgentRuntimeNotFoundError,
)
from openhands.core.logger import openhands_logger as logger
from openhands.events import EventStream
from openhands.integrations.provider import PROVIDER_TOKEN_TYPE
from openhands.llm.llm_registry import LLMRegistry
from openhands.runtime.impl.action_execution.action_execution_client import (
    ActionExecutionClient,
)
from openhands.runtime.plugins import PluginRequirement
from openhands.runtime.runtime_status import RuntimeStatus
from openhands.runtime.utils import find_available_tcp_port
from openhands.runtime.utils.command import (
    DEFAULT_MAIN_MODULE,
    get_action_execution_server_startup_command,
)
from openhands.runtime.utils.port_lock import PortLock, find_available_port_with_lock
from openhands.utils.async_utils import call_sync_from_async


EXECUTION_SERVER_PORT_RANGE = (30000, 39999)
VSCODE_PORT_RANGE = (40000, 49999)
APP_PORT_RANGE_1 = (50000, 54999)
APP_PORT_RANGE_2 = (55000, 59999)


class ApptainerRuntime(ActionExecutionClient):
    """Runtime implementation backed by Apptainer/Singularity containers."""

    def __init__(
        self,
        config: OpenHandsConfig,
        event_stream: EventStream,
        llm_registry: LLMRegistry,
        sid: str = 'default',
        plugins: list[PluginRequirement] | None = None,
        env_vars: dict[str, str] | None = None,
        status_callback: Callable | None = None,
        attach_to_existing: bool = False,
        headless_mode: bool = True,
        user_id: str | None = None,
        git_provider_tokens: PROVIDER_TOKEN_TYPE | None = None,
        main_module: str = DEFAULT_MAIN_MODULE,
    ):
        self.main_module = main_module
        self._server_process: subprocess.Popen[str] | None = None
        self._log_thread: threading.Thread | None = None
        self._log_file_handle: io.TextIOWrapper | None = None
        self._log_file_path: str | None = None
        self._host_port_lock: PortLock | None = None
        self._vscode_port_lock: PortLock | None = None
        self._app_port_locks: list[PortLock | None] = []
        self._host_port: int = -1
        self._container_port: int = -1
        self._vscode_port: int | None = None
        self._app_ports: list[int] = []
        self._apptainer_executable = self._detect_apptainer_executable()
        self._log_dir = os.environ.get('APPTAINER_RUNTIME_LOG_DIR')
        if self._log_dir:
            Path(self._log_dir).mkdir(parents=True, exist_ok=True)

        self.apptainer_image = self._normalize_image_reference(
            config.sandbox.runtime_container_image or config.sandbox.base_container_image
        )
        if not self.apptainer_image:
            raise ValueError(
                'Apptainer runtime requires sandbox.runtime_container_image or base_container_image to be set.'
            )

        self._host_port, self._host_port_lock = self._allocate_port(
            EXECUTION_SERVER_PORT_RANGE
        )
        self._container_port = self._host_port

        self._vscode_port, self._vscode_port_lock = self._allocate_port(
            VSCODE_PORT_RANGE
        )

        for port_range in (APP_PORT_RANGE_1, APP_PORT_RANGE_2):
            port, lock = self._allocate_port(port_range)
            self._app_ports.append(port)
            self._app_port_locks.append(lock)

        self.api_url = f"{config.sandbox.local_runtime_url}:{self._container_port}"

        super().__init__(
            config,
            event_stream,
            llm_registry,
            sid,
            plugins,
            env_vars,
            status_callback,
            attach_to_existing,
            headless_mode,
            user_id,
            git_provider_tokens,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self.set_runtime_status(RuntimeStatus.STARTING_RUNTIME)
        await call_sync_from_async(self._ensure_process_started)
        await self._wait_until_alive()

        if not self.attach_to_existing:
            await call_sync_from_async(self.setup_initial_env)

        self.set_runtime_status(RuntimeStatus.READY)
        self._runtime_initialized = True

    def close(self) -> None:
        super().close()
        if self._server_process:
            if self._server_process.poll() is None:
                self._server_process.terminate()
                try:
                    self._server_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._server_process.kill()
                    self._server_process.wait(timeout=5)
            if self._server_process.stdout:
                try:
                    self._server_process.stdout.close()
                except Exception:
                    pass
            self._server_process = None

        if self._log_thread and self._log_thread.is_alive():
            self._log_thread.join(timeout=1)
        self._log_thread = None
        if self._log_file_handle:
            try:
                self._log_file_handle.close()
            except Exception:
                pass
            self._log_file_handle = None
        self._release_port_locks()

    @classmethod
    async def delete(cls, conversation_id: str) -> None:  # pragma: no cover
        # Apptainer runtime does not track containers by conversation ID.
        return None

    # ------------------------------------------------------------------
    # Runtime plumbing
    # ------------------------------------------------------------------

    def _ensure_process_started(self) -> None:
        if self._server_process and self._server_process.poll() is None:
            return
        if self.attach_to_existing:
            raise AgentRuntimeNotFoundError(
                'attach_to_existing is not supported for Apptainer runtime.'
            )
        self._start_apptainer_process()

    def _start_apptainer_process(self) -> None:
        command, env = self._build_apptainer_command()
        self.log('info', f'Starting Apptainer runtime with image: {self.apptainer_image}')
        if self._log_dir:
            self._log_file_path = os.path.join(self._log_dir, f'{self.sid}.log')
        self._server_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        if self._log_file_path:
            try:
                self._log_file_handle = open(
                    self._log_file_path, 'a', encoding='utf-8', buffering=1
                )
                self.log('debug', f'Apptainer runtime log file: {self._log_file_path}')
            except OSError as exc:
                self.log(
                    'warning',
                    f'Failed to open Apptainer runtime log file {self._log_file_path}: {exc}',
                )
                self._log_file_handle = None
        self._start_log_thread()

    def _start_log_thread(self) -> None:
        if not self._server_process or not self._server_process.stdout:
            return

        def _log_output() -> None:
            assert self._server_process and self._server_process.stdout
            for line in self._server_process.stdout:
                if not line:
                    continue
                self.log('debug', f'[apptainer] {line.rstrip()}')
                if self._log_file_handle:
                    try:
                        self._log_file_handle.write(line)
                    except Exception:
                        pass
            try:
                self._server_process.stdout.close()
            except Exception:
                pass

        self._log_thread = threading.Thread(target=_log_output, daemon=True)
        self._log_thread.start()

    async def _wait_until_alive(self) -> None:
        deadline = time.time() + self.config.sandbox.remote_runtime_init_timeout
        last_error: Exception | None = None
        while time.time() < deadline:
            if self._server_process and self._server_process.poll() is not None:
                raise AgentRuntimeDisconnectedError(
                    'Apptainer runtime process exited unexpectedly.'
                )
            try:
                self.check_if_alive()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(1)
        raise AgentRuntimeDisconnectedError(
            'Timed out waiting for Apptainer runtime to become ready.'
        ) from last_error

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _normalize_image_reference(self, image: str | None) -> str:
        if not image:
            return ''
        if image.startswith(('docker://', 'library://', 'oras://')):
            return image
        if image.endswith('.sif') or image.startswith('/') or image.startswith('.'):
            return image
        if '://' not in image and '/' in image:
            return f'docker://{image}'
        return image


    def _build_apptainer_command(self) -> tuple[list[str], dict[str, str]]:
        sandbox = self.config.sandbox
        command: list[str] = [self._apptainer_executable, 'exec', '--cleanenv']

        for bind in self._build_bind_args():
            command.extend(['--bind', bind])

        if sandbox.enable_gpu:
            command.append('--nv')

        command.extend(['--pwd', '/'])

        env = os.environ.copy()
        env_updates = {
            'port': str(self._container_port),
            'PYTHONUNBUFFERED': '1',
        }
        if self._vscode_port is not None:
            env_updates['VSCODE_PORT'] = str(self._vscode_port)
        if len(self._app_ports) >= 2:
            env_updates['APP_PORT_1'] = str(self._app_ports[0])
            env_updates['APP_PORT_2'] = str(self._app_ports[1])

        for key, value in env_updates.items():
            env[f'APPTAINERENV_{key}'] = value
            env[f'SINGULARITYENV_{key}'] = value

        command.append(self.apptainer_image)
        command.extend(self.get_action_execution_server_startup_command())

        self.log('debug', f'Apptainer exec command: {command}')
        return command, env

    def _build_bind_args(self) -> list[str]:
        binds: list[str] = []
        sandbox = self.config.sandbox
        if sandbox.volumes:
            mounts = [entry.strip() for entry in sandbox.volumes.split(',') if entry.strip()]
            for mount in mounts:
                parts = mount.split(':')
                if len(parts) < 2:
                    continue
                host_path = os.path.abspath(parts[0])
                container_path = parts[1]
                mode = parts[2] if len(parts) > 2 else 'rw'
                if 'overlay' in mode:
                    self.log(
                        'warning',
                        f'Overlay mount "{mount}" is not supported in Apptainer runtime; skipping.',
                    )
                    continue
                binds.append(f'{host_path}:{container_path}:{mode}')
        elif (
            self.config.workspace_mount_path is not None
            and self.config.workspace_mount_path_in_sandbox is not None
        ):
            host_path = os.path.abspath(self.config.workspace_mount_path)
            container_path = self.config.workspace_mount_path_in_sandbox
            binds.append(f'{host_path}:{container_path}:rw')
        return binds

    def _allocate_port(self, port_range: tuple[int, int]) -> tuple[int, PortLock | None]:
        result = find_available_port_with_lock(
            min_port=port_range[0],
            max_port=port_range[1],
            bind_address='0.0.0.0',
            lock_timeout=1.0,
        )
        if result is not None:
            return result
        port = find_available_tcp_port(port_range[0], port_range[1])
        return port, None

    def _release_port_locks(self) -> None:
        if self._host_port_lock:
            self._host_port_lock.release()
            self._host_port_lock = None
        if self._vscode_port_lock:
            self._vscode_port_lock.release()
            self._vscode_port_lock = None
        for lock in self._app_port_locks:
            if lock:
                lock.release()
        self._app_port_locks.clear()

    def _detect_apptainer_executable(self) -> str:
        candidate = os.environ.get('APPTAINER_EXECUTABLE')
        if candidate and shutil.which(candidate):
            return candidate
        for binary in ('apptainer', 'singularity'):
            path = shutil.which(binary)
            if path:
                return path
        raise FileNotFoundError(
            'Apptainer runtime requires either "apptainer" or "singularity" executable in PATH.'
        )

    # ------------------------------------------------------------------
    # Runtime API integration
    # ------------------------------------------------------------------

    @property
    def action_execution_server_url(self) -> str:
        return self.api_url

    @property
    def vscode_url(self) -> str | None:  # pragma: no cover - headless default
        return None

    @property
    def web_hosts(self) -> dict[str, int]:  # pragma: no cover - not used in eval
        return {}

    def get_action_execution_server_startup_command(self) -> list[str]:
        return get_action_execution_server_startup_command(
            server_port=self._container_port,
            plugins=self.plugins,
            app_config=self.config,
            main_module=self.main_module,
        )
