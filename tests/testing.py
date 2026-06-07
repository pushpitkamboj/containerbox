from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from unittest.mock import patch

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from containerbox import SandboxError, SandboxResult, SandboxSession


TEST_IMAGE = "python:3.13-slim"
NODE_IMAGE = "containerbox-node-extra:test"
ROOT = Path(__file__).resolve().parents[1]


def assert_raises(exc_type: type[BaseException], fn: Callable[[], object]) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def docker_client() -> docker.DockerClient:
    client = docker.from_env()
    client.ping()
    return client


def require_image(image: str) -> None:
    client = docker_client()
    try:
        client.images.get(image)
    except ImageNotFound as exc:
        raise RuntimeError(f"missing test image {image!r}; run: docker pull {image}") from exc
    finally:
        client.close()


def build_node_image() -> None:
    client = docker_client()
    try:
        client.images.build(
            path=str(ROOT),
            dockerfile="tests/Dockerfile.node",
            tag=NODE_IMAGE,
            rm=True,
        )
    finally:
        client.close()


def assert_container_removed(container_id: str) -> None:
    client = docker_client()
    try:
        assert_raises(NotFound, lambda: client.containers.get(container_id))
    finally:
        client.close()


def test_constructor_validation() -> None:
    session = SandboxSession()
    assert session.runtime == "docker"
    assert session.workdir == "/workspace"
    assert session.session_timeout is None
    assert session._backend.image == "ubuntu:24.04"
    assert not session.is_open

    assert_raises(ValueError, lambda: SandboxSession(runtime="podman"))
    assert_raises(ValueError, lambda: SandboxSession(workdir="workspace"))
    assert_raises(ValueError, lambda: SandboxSession(workdir="/tmp/../workspace"))


def test_open_pulls_missing_image_before_create() -> None:
    events: list[str] = []

    class FakeContainer:
        id = "fake-container"
        status = "created"

        def reload(self) -> None:
            events.append("reload")

        def remove(self, *, force: bool = False) -> None:
            events.append(f"remove:{force}")

    class FakeImages:
        def get(self, image: str) -> None:
            events.append(f"get:{image}")
            raise ImageNotFound("missing")

        def pull(self, image: str) -> None:
            events.append(f"pull:{image}")

    class FakeContainers:
        def create(self, **options: object) -> FakeContainer:
            events.append(f"create:{options['image']}")
            return FakeContainer()

    class FakeClient:
        images = FakeImages()
        containers = FakeContainers()

        def close(self) -> None:
            events.append("close")

    with patch("containerbox.session.docker.from_env", return_value=FakeClient()):
        session = SandboxSession("missing:test")
        try:
            session.open()
            assert session.is_open
        finally:
            session.close()

    assert events[:3] == ["get:missing:test", "pull:missing:test", "create:missing:test"]


def test_enter_creates_and_exit_removes_container() -> None:
    with SandboxSession(TEST_IMAGE) as session:
        assert session.is_open
        container = session._backend.require_container()
        container_id = container.id
        container.reload()
        assert container.status == "created"

    assert not session.is_open
    assert_container_removed(container_id)


def test_manual_open_close_lifecycle() -> None:
    session = SandboxSession(TEST_IMAGE)
    assert session.open() is session
    assert session.is_open

    first_container_id = session._backend.require_container().id
    assert session.open() is session
    assert session._backend.require_container().id == first_container_id

    result = session.exec("echo manual")
    assert_result(result, stdout="manual\n", stderr="", exit_code=0)

    session.close()
    assert not session.is_open
    assert_container_removed(first_container_id)

    session.close()
    assert not session.is_open


def test_exec_result_success_failure_and_timeout() -> None:
    with SandboxSession(TEST_IMAGE) as session:
        result = session.exec("printf 'out'; printf 'err' >&2")
        assert_result(result, stdout="out", stderr="err", exit_code=0)
        assert result.ok
        assert isinstance(result.duration_ms, int)
        assert result.duration_ms >= 0

        listed = session.exec(["python", "-c", "print(20 + 22)"])
        assert_result(listed, stdout="42\n", stderr="", exit_code=0)

        failed = session.exec("python -c \"import sys; sys.stderr.write('bad'); sys.exit(7)\"")
        assert_result(failed, stdout="", stderr="bad", exit_code=7)
        assert not failed.ok
        assert not failed.timed_out

        timed_out = session.exec("sleep 3", timeout=1)
        assert timed_out.exit_code == 124
        assert timed_out.timed_out
        assert not timed_out.ok


def test_run_code_writes_main_py_and_supports_custom_command() -> None:
    with SandboxSession(TEST_IMAGE) as session:
        result = session.run_code("from pathlib import Path\nprint(Path('main.py').read_text())")
        assert_result(
            result,
            stdout="from pathlib import Path\nprint(Path('main.py').read_text())\n",
            stderr="",
            exit_code=0,
        )

        exists = session.exec("test -f /workspace/main.py")
        assert exists.exit_code == 0

        custom = session.run_code(
            "echo custom-shell",
            filename="script.sh",
            command="sh script.sh",
        )
        assert_result(custom, stdout="custom-shell\n", stderr="", exit_code=0)

        assert_raises(ValueError, lambda: session.run_code("print('x')", filename="../main.py"))


def test_custom_node_dockerfile_image_runs_js() -> None:
    build_node_image()

    code = """
const { slug } = require("slugify-mini");
console.log(slug("Hello from Custom Node Image!"));
console.error(process.version);
""".strip()

    with SandboxSession(NODE_IMAGE) as session:
        result = session.run_code(
            code,
            filename="main.js",
            command=["node", "main.js"],
            timeout=2,
        )

    assert result.exit_code == 0, result
    assert result.stdout == "hello-from-custom-node-image\n", result
    assert result.stderr.startswith("v22."), result
    assert not result.timed_out


def test_upload_download_files_and_folders() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        local_file = root / "hello.txt"
        local_file.write_text("hello")

        folder = root / "fixture"
        folder.mkdir()
        (folder / "a.txt").write_text("alpha")
        (folder / "b.txt").write_text("beta")

        downloaded_file = root / "downloaded.txt"
        downloaded_dir = root / "downloaded_dir"

        with SandboxSession(TEST_IMAGE) as session:
            assert session.upload(local_file) == "/workspace/hello.txt"
            assert session.exec("cat hello.txt").stdout == "hello"

            assert session.upload(local_file, "nested/renamed.txt") == "/workspace/nested/renamed.txt"
            assert session.exec("cat nested/renamed.txt").stdout == "hello"

            assert session.upload(folder) == "/workspace/fixture"
            assert session.exec("cat fixture/a.txt fixture/b.txt").stdout == "alphabeta"

            assert session.download("hello.txt") == b"hello"
            assert session.download("hello.txt", downloaded_file) == downloaded_file
            assert downloaded_file.read_text() == "hello"

            assert session.download("fixture", downloaded_dir) == downloaded_dir
            assert (downloaded_dir / "fixture" / "a.txt").read_text() == "alpha"
            assert (downloaded_dir / "fixture" / "b.txt").read_text() == "beta"

            assert_raises(SandboxError, lambda: session.download("fixture"))
            assert_raises(FileNotFoundError, lambda: session.upload(root / "missing.txt"))
            assert_raises(ValueError, lambda: session.upload(local_file, "../escape.txt"))
            assert_raises(ValueError, lambda: session.download("../escape.txt"))
            assert_raises(ValueError, lambda: session.download("/etc/passwd"))


def test_secureish_defaults_and_limits() -> None:
    with SandboxSession(TEST_IMAGE, memory="128m", cpus=0.5) as session:
        container = session._backend.require_container()
        container.reload()
        attrs = container.attrs

        assert attrs["HostConfig"]["Memory"] == 128 * 1024 * 1024
        assert attrs["HostConfig"]["NanoCpus"] == 500_000_000
        assert attrs["Config"].get("NetworkDisabled") is True
        assert all("docker.sock" not in str(mount) for mount in attrs.get("Mounts", []))

        user = session.exec("id -u")
        assert user.stdout.strip() == "1000"


def test_session_timeout_closes_sandbox() -> None:
    session = SandboxSession(TEST_IMAGE, session_timeout=1)
    with session:
        container_id = session._backend.require_container().id
        time.sleep(1.2)
        exc = assert_raises(SandboxError, lambda: session.exec("echo late"))
        assert "session timed out" in str(exc)

    assert_container_removed(container_id)


def test_invalid_usage_edges() -> None:
    session = SandboxSession(TEST_IMAGE)
    assert_raises(RuntimeError, lambda: session.exec("echo no-context"))

    assert_raises(
        SandboxError,
        lambda: SandboxSession("containerbox-no-such-image:latest").open(),
    )


def assert_result(
    result: SandboxResult,
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
) -> None:
    assert result.stdout == stdout, result
    assert result.stderr == stderr, result
    assert result.exit_code == exit_code, result
    assert result.timed_out is False, result


TESTS = [
    test_constructor_validation,
    test_open_pulls_missing_image_before_create,
    test_enter_creates_and_exit_removes_container,
    test_manual_open_close_lifecycle,
    test_exec_result_success_failure_and_timeout,
    test_run_code_writes_main_py_and_supports_custom_command,
    test_custom_node_dockerfile_image_runs_js,
    test_upload_download_files_and_folders,
    test_secureish_defaults_and_limits,
    test_session_timeout_closes_sandbox,
    test_invalid_usage_edges,
]


def main() -> None:
    require_image(TEST_IMAGE)

    for test in TESTS:
        test()
        print(f"PASS {test.__name__}")

    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    try:
        main()
    except DockerException as exc:
        raise SystemExit(f"Docker is not reachable: {exc}") from exc
