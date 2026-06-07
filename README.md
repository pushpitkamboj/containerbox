# ContainerBox

Minimal Docker-only sandbox for running generated code.

Missing images are pulled automatically before the sandbox is created.

## Usage

Use the context manager when the sandbox belongs to one block of work:

```python
from containerbox import SandboxSession

with SandboxSession() as session:
    result = session.exec("echo hi")
    print(result.stdout)
```

Use manual lifecycle when you need to pass the same sandbox across functions or modules:

```python
from containerbox import SandboxSession

session = SandboxSession("python:3.13-slim")
session.open()

try:
    result = session.run_code("print('hi')", timeout=5)
    print(result.stdout)
finally:
    session.close()
```

When using manual lifecycle, always call `close()`. The context manager does this for you; manual mode makes cleanup your responsibility.

## API

```python
with SandboxSession(
    image="ubuntu:24.04",
    runtime="docker",
    session_timeout=300,
    memory="256m",
    cpus=1.0,
    network=False,
) as session:
    result = session.exec("echo ready", timeout=10)
```

For Python code, use a Python image:

```python
with SandboxSession("python:3.13-slim") as session:
    result = session.run_code("print('hi')", timeout=5)
```

`SandboxResult` contains:

- `stdout`
- `stderr`
- `exit_code`
- `timed_out`
- `duration_ms`

## Files

```python
with SandboxSession("python:3.13-slim") as session:
    session.upload("local_data.csv")
    result = session.run_code("print(open('local_data.csv').read())")
    session.download("main.py", "downloaded_main.py")
```

## Custom Image

```bash
docker build -f tests/Dockerfile.node -t containerbox-node-extra:test .
```

```python
from containerbox import SandboxSession

code = """
const { slug } = require("slugify-mini");
console.log(slug("Hello from Custom Node Image!"));
"""

with SandboxSession("containerbox-node-extra:test") as session:
    result = session.run_code(code, filename="main.js", command=["node", "main.js"])
    print(result.stdout)
```
