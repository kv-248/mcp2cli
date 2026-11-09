"""``--head N`` must truncate array results on every source, not only some.

The flag is documented for OpenAPI (``mcp2cli --spec ./spec.json --head 5
list-records``) and the helpers that print a result already honor it. The
call sites for OpenAPI, GraphQL, MCP tool calls, and session output dropped
the value, so the flag was accepted and then ignored.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

MCP_SERVER = str(Path(__file__).parent / "mcp_test_server.py")


def _run(args, *, env=None, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


@pytest.fixture
def records_endpoint(tmp_path):
    """OpenAPI server: ``/records`` is an array, ``/record`` is one object."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/record":
                body = {"id": 7, "tags": ["a", "b", "c"]}
            else:
                body = [1, 2, 3, 4]
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(
        json.dumps(
            {
                "openapi": "3.0.4",
                "info": {"title": "Records", "version": "1"},
                "servers": [{"url": base}],
                "paths": {
                    "/records": {
                        "get": {
                            "operationId": "listRecords",
                            "responses": {"200": {"description": "records"}},
                        }
                    },
                    "/record": {
                        "get": {
                            "operationId": "getRecord",
                            "responses": {"200": {"description": "one record"}},
                        }
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    try:
        yield spec_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_head_truncates_openapi_array_and_leaves_objects(records_endpoint):
    spec = str(records_endpoint)
    full = _run(["--spec", spec, "list-records"])
    assert full.returncode == 0, full.stderr
    assert json.loads(full.stdout) == [1, 2, 3, 4]

    limited = _run(["--spec", spec, "--head", "2", "list-records"])
    assert limited.returncode == 0, limited.stderr
    assert json.loads(limited.stdout) == [1, 2]

    as_json = _run(["--spec", spec, "--json", "--head", "1", "list-records"])
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == [1]

    # --head only slices arrays. An object response stays intact, including
    # lists nested inside it.
    obj = _run(["--spec", spec, "--head", "1", "get-record"])
    assert obj.returncode == 0, obj.stderr
    assert json.loads(obj.stdout) == {"id": 7, "tags": ["a", "b", "c"]}


def test_head_truncates_graphql_list(graphql_server):
    full = _run(["--graphql", graphql_server, "users"])
    assert full.returncode == 0, full.stderr
    records = json.loads(full.stdout)
    if len(records) < 2:
        for name in ("HeadA", "HeadB"):
            created = _run(
                [
                    "--graphql",
                    graphql_server,
                    "create-user",
                    "--name",
                    name,
                    "--email",
                    f"{name.lower()}@example.com",
                    "--age",
                    "20",
                ]
            )
            assert created.returncode == 0, created.stderr
        full = _run(["--graphql", graphql_server, "users"])
        assert full.returncode == 0, full.stderr
        records = json.loads(full.stdout)
    assert len(records) >= 2

    limited = _run(["--graphql", graphql_server, "--head", "1", "users"])
    assert limited.returncode == 0, limited.stderr
    assert json.loads(limited.stdout) == records[:1]


def test_head_truncates_mcp_tool_array():
    message = "[1,2,3,4]"
    full = _run(
        ["--mcp-stdio", f"{sys.executable} {MCP_SERVER}", "echo", "--message", message]
    )
    assert full.returncode == 0, full.stderr
    assert json.loads(full.stdout) == [1, 2, 3, 4]

    limited = _run(
        [
            "--mcp-stdio",
            f"{sys.executable} {MCP_SERVER}",
            "--head",
            "2",
            "echo",
            "--message",
            message,
        ]
    )
    assert limited.returncode == 0, limited.stderr
    assert json.loads(limited.stdout) == [1, 2]


def test_head_truncates_session_output(tmp_path):
    env = os.environ.copy()
    env["MCP2CLI_CACHE_DIR"] = str(tmp_path / "cache")
    name = "head-limit"
    server = f"{sys.executable} {MCP_SERVER}"
    started = _run(
        ["--mcp-stdio", server, "--session-start", name],
        env=env,
    )
    assert started.returncode == 0, started.stderr
    try:
        resources = _run(["--session", name, "--list-resources"], env=env)
        assert resources.returncode == 0, resources.stderr
        all_resources = json.loads(resources.stdout)
        assert len(all_resources) >= 2

        limited = _run(
            ["--session", name, "--head", "1", "--list-resources"],
            env=env,
        )
        assert limited.returncode == 0, limited.stderr
        assert json.loads(limited.stdout) == all_resources[:1]

        echoed = _run(
            [
                "--session",
                name,
                "--head",
                "2",
                "echo",
                "--message",
                "[1,2,3,4]",
            ],
            env=env,
        )
        assert echoed.returncode == 0, echoed.stderr
        assert json.loads(echoed.stdout) == [1, 2]
    finally:
        _run(["--session-stop", name], env=env)
