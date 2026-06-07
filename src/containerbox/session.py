from __future__ import annotations

import io
import shlex
import tarfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

import docker
from docker.errors import DockerException, ImageNotFound
from docker.models.containers import Container


Command = str | Sequence[str]


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class _DockerBackend:
    def __init__(
        self,
        *,
        image: str,
        command: Command,
        workdir: str,
        memory: str | int | None = None,
        cpus: float | None = None,
        network: bool,
    ) -> None:
        self.image = image
        self.command = command
        self.workdir = workdir
        self.memory = memory
        self.cpus = cpus
        self.network = network
        self.client: docker.DockerClient | None = None
        self.container: Container | None = None

    def create(self) -> None:
        self.client = docker.from_env()
        self._pull_missing_image()

        self.container = self.client.containers.create(**self._create_options())

    def _pull_missing_image(self) -> None:
        if self.client is None:
            return

        try:
            self.client.images.get(self.image)
        except ImageNotFound:
            self.client.images.pull(self.image)

    def _create_options(self) -> dict[str, object]:
        options = {
            "image": self.image,
            "command": self.command,
            "detach": True,
            "working_dir": self.workdir,
            "network_disabled": not self.network,
            "security_opt": ["no-new-privileges"],
        }
        if self.memory is not None:
            options["mem_limit"] = self.memory
        if self.cpus is not None:
            options["nano_cpus"] = int(self.cpus * 1_000_000_000)

        return options

    def start(self) -> None:
        container = self.require_container()
        container.reload()
        if container.status != "running":
            container.start()

    def exec(self, cmd: Command, *, workdir: str, user: str | None = None):
        return self.require_container().exec_run(
            cmd,
            workdir=workdir,
            user=user or "",
            stdout=True,
            stderr=True,
            demux=True,
        )

    def put_archive(self, path: str, data: bytes) -> None:
        self.require_container().put_archive(path, data)

    def get_archive(self, path: str) -> bytes:
        stream, _ = self.require_container().get_archive(path)
        return b"".join(stream)

    def close(self) -> None:
        try:
            if self.container is not None:
                with suppress(DockerException):
                    self.container.reload()
                    if self.container.status == "running":
                        self.container.stop(timeout=1)
                with suppress(DockerException):
                    self.container.remove(force=True)
                self.container = None
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None

    def require_container(self) -> Container:
        if self.container is None:
            raise RuntimeError("SandboxSession must be used as a context manager")
        return self.container


class SandboxSession:
    def __init__(
        self,
        image: str = "ubuntu:24.04",
        *,
        runtime: str | None = "docker",
        workdir: str = "/workspace",
        session_timeout: int | None = None,
        memory: str | int | None = "256m",
        cpus: float | None = 1.0,
        network: bool = False,
        user: str | None = "1000:1000",
        command: Command = ("sleep", "infinity"),
    ) -> None:
        if runtime not in (None, "docker"):
            raise ValueError("Only the Docker runtime is supported")

        self.runtime = runtime
        self.workdir = self._workdir_path(workdir)
        self.session_timeout = session_timeout
        self._started_at: float | None = None
        self.user = user
        self._workspace_ready = False
        self._backend = _DockerBackend(
            image=image,
            command=command,
            workdir=self.workdir,
            memory=memory,
            cpus=cpus,
            network=network,
        )

    @property
    def is_open(self) -> bool:
        return self._backend.container is not None

    def open(self) -> SandboxSession:
        if self.is_open:
            return self

        try:
            self._backend.create()
            self._started_at = time.monotonic()
        except DockerException as exc:
            self._backend.close()
            raise SandboxError(f"failed to create sandbox: {exc}") from exc
        return self

    def close(self) -> None:
        self._backend.close()
        self._started_at = None
        self._workspace_ready = False

    def __enter__(self) -> SandboxSession:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def exec(self, command: Command, *, timeout: int | None = None) -> SandboxResult:
        self._check_session_timeout()
        self._start()
        cmd = self._with_timeout(command, timeout)
        started = time.perf_counter()

        try:
            result = self._backend.exec(cmd, workdir=self.workdir, user=self.user)
        except DockerException as exc:
            raise SandboxError(f"failed to execute command: {exc}") from exc

        duration_ms = round((time.perf_counter() - started) * 1000)
        stdout, stderr = self._decode(result.output)
        exit_code = result.exit_code or 0

        return SandboxResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            timed_out=exit_code == 124,
            duration_ms=duration_ms,
        )

    def run_code(
        self,
        code: str,
        *,
        filename: str = "main.py",
        command: Command | None = None,
        timeout: int | None = None,
    ) -> SandboxResult:
        self._check_session_timeout()
        target = self._container_path(filename)
        self._put_bytes(code.encode(), target)
        return self.exec(command or ["python", PurePosixPath(target).name], timeout=timeout)

    def upload(self, src: str | Path, dest: str | None = None) -> str:
        self._check_session_timeout()
        self._start()
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(src_path)

        target = self._container_path(dest or src_path.name)
        archive = self._tar_path(src_path, PurePosixPath(target).name)
        self._mkdir(str(PurePosixPath(target).parent))

        try:
            self._backend.put_archive(str(PurePosixPath(target).parent), archive)
        except DockerException as exc:
            raise SandboxError(f"failed to upload {src_path}: {exc}") from exc

        self._chown(target)
        return target

    def download(self, src: str, dest: str | Path | None = None) -> bytes | Path:
        self._check_session_timeout()
        self._start()
        source = self._container_path(src)

        try:
            archive = self._backend.get_archive(source)
        except DockerException as exc:
            raise SandboxError(f"failed to download {source}: {exc}") from exc

        if dest is None:
            return self._single_file_bytes(archive, source)

        dest_path = Path(dest)
        self._extract_archive(archive, dest_path)
        return dest_path

    def _start(self) -> None:
        self._check_session_timeout()
        try:
            self._backend.start()
            if not self._workspace_ready:
                self._mkdir(self.workdir)
                self._chown(self.workdir)
                self._workspace_ready = True
        except DockerException as exc:
            raise SandboxError(f"failed to start sandbox: {exc}") from exc

    def _mkdir(self, path: str) -> None:
        self._backend.exec(["sh", "-lc", f"mkdir -p {shlex.quote(path)}"], workdir="/", user="root")

    def _chown(self, path: str) -> None:
        if self.user:
            self._backend.exec(
                ["sh", "-lc", f"chown -R {shlex.quote(self.user)} {shlex.quote(path)} || true"],
                workdir="/",
                user="root",
            )

    def _put_bytes(self, content: bytes, dest: str) -> None:
        self._start()
        dest_path = PurePosixPath(dest)
        self._mkdir(str(dest_path.parent))
        archive = io.BytesIO()

        with tarfile.open(fileobj=archive, mode="w") as tar:
            info = tarfile.TarInfo(dest_path.name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))

        try:
            self._backend.put_archive(str(dest_path.parent), archive.getvalue())
        except DockerException as exc:
            raise SandboxError(f"failed to write {dest}: {exc}") from exc

        self._chown(dest)

    def _with_timeout(self, command: Command, timeout: int | None) -> Command:
        if isinstance(command, str):
            if timeout and timeout > 0:
                return ["timeout", f"{timeout}s", "sh", "-lc", command]
            return ["sh", "-lc", command]
        if not timeout or timeout <= 0:
            return command
        return ["timeout", f"{timeout}s", *command]

    def _check_session_timeout(self) -> None:
        if self.session_timeout is None or self._started_at is None:
            return
        if time.monotonic() - self._started_at <= self.session_timeout:
            return

        self._backend.close()
        raise SandboxError(f"sandbox session timed out after {self.session_timeout}s")

    def _container_path(self, path: str) -> str:
        value = PurePosixPath(path)
        if ".." in value.parts:
            raise ValueError(f"path traversal is not allowed: {path}")
        root = PurePosixPath(self.workdir)
        if value.is_absolute():
            if value != root and root not in value.parents:
                raise ValueError(f"path must stay inside {self.workdir}: {path}")
        else:
            value = root / value
        return value.as_posix()

    @staticmethod
    def _workdir_path(path: str) -> str:
        value = PurePosixPath(path)
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("workdir must be an absolute container path")
        return value.as_posix()

    @staticmethod
    def _decode(output: object) -> tuple[str, str]:
        if not isinstance(output, tuple):
            return "", ""

        stdout, stderr = output
        return (
            (stdout or b"").decode("utf-8", errors="replace"),
            (stderr or b"").decode("utf-8", errors="replace"),
        )

    @staticmethod
    def _tar_path(src: Path, arcname: str) -> bytes:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(src, arcname=arcname)
        return archive.getvalue()

    @staticmethod
    def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
        members = []
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                continue
            members.append(member)
        return members

    def _single_file_bytes(self, archive: bytes, src: str) -> bytes:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            files = [member for member in self._safe_members(tar) if member.isfile()]
            if len(files) != 1:
                raise SandboxError(f"{src} is not a single file")
            file_obj = tar.extractfile(files[0])
            if file_obj is None:
                raise SandboxError(f"failed to read {src}")
            return file_obj.read()

    def _extract_archive(self, archive: bytes, dest: Path) -> None:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            members = self._safe_members(tar)
            files = [member for member in members if member.isfile()]

            if len(members) == 1 and len(files) == 1 and not dest.is_dir():
                dest.parent.mkdir(parents=True, exist_ok=True)
                file_obj = tar.extractfile(files[0])
                if file_obj is None:
                    raise SandboxError(f"failed to extract {files[0].name}")
                dest.write_bytes(file_obj.read())
                return

            dest.mkdir(parents=True, exist_ok=True)
            root = dest.resolve()
            for member in members:
                target = (root / member.name).resolve()
                if root not in (target, *target.parents):
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    file_obj = tar.extractfile(member)
                    if file_obj is not None:
                        target.write_bytes(file_obj.read())


__all__ = ["SandboxError", "SandboxResult", "SandboxSession"]
