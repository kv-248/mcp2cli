"""OpenAPI server URLs are templates: {variables} must be filled before a request."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _EchoHandler(BaseHTTPRequestHandler):
    """Echoes the request path. Serves /openapi.json from the spec set on the class."""

    spec = None

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/openapi.json" and self.spec is not None:
            body = json.dumps(self.spec).encode()
        else:
            body = json.dumps({"path": self.path.split("?", 1)[0]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def echo_server():
    _EchoHandler.spec = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        _EchoHandler.spec = None
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run(args, tmp_path):
    env = os.environ.copy()
    env["MCP2CLI_CACHE_DIR"] = str(tmp_path / "cache")
    return subprocess.run(
        [sys.executable, "-m", "mcp2cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=env,
    )


def test_absolute_server_url_variables_are_substituted(tmp_path, echo_server):
    """A templated server URL is requested with each variable's default filled in."""
    port = echo_server.server_address[1]
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Templated server", "version": "1"},
        "servers": [
            {
                "url": f"http://127.0.0.1:{port}/{{version}}/{{team}}",
                "variables": {
                    "version": {"default": "v1", "enum": ["v1", "v2"]},
                    "team": {"default": "widgets"},
                },
            }
        ],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = _run(["--spec", str(spec_path), "--json", "list-items"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == "/v1/widgets/items"


def test_relative_server_url_variables_are_substituted(tmp_path, echo_server):
    """A relative server template is expanded, then joined to the spec's origin."""
    port = echo_server.server_address[1]
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Relative template", "version": "1"},
        "servers": [
            {
                "url": "/{version}",
                "variables": {"version": {"default": "v1"}},
            }
        ],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    _EchoHandler.spec = spec
    result = _run(
        ["--spec", f"http://127.0.0.1:{port}/openapi.json", "--json", "list-items"],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == "/v1/items"


def test_explicit_base_url_is_not_rewritten(tmp_path, echo_server):
    """--base-url is the request root even when the spec server URL is a template."""
    port = echo_server.server_address[1]
    spec = {
        "openapi": "3.0.4",
        "info": {"title": "Override", "version": "1"},
        "servers": [
            {
                "url": "https://example.invalid/{version}",
                "variables": {"version": {"default": "v1"}},
            }
        ],
        "paths": {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    spec_path = tmp_path / "openapi.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    result = _run(
        [
            "--spec",
            str(spec_path),
            "--base-url",
            f"http://127.0.0.1:{port}",
            "--json",
            "list-items",
        ],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["path"] == "/items"


def test_server_variable_defaults_match_the_spec_example():
    """The documented server template substitutes every default, and nothing else."""
    from mcp2cli import _expand_openapi_server_url

    server = {
        "url": "https://{username}.gigantic-server.com:{port}/{basePath}",
        "variables": {
            "username": {"default": "demo"},
            "port": {"enum": ["8443", "443"], "default": "8443"},
            "basePath": {"default": "v2"},
        },
    }
    assert (
        _expand_openapi_server_url(server)
        == "https://demo.gigantic-server.com:8443/v2"
    )


@pytest.mark.parametrize(
    "server,expected",
    [
        ({"url": "https://api.example.com/v1"}, "https://api.example.com/v1"),
        ({"url": "/api/v1"}, "/api/v1"),
        # YAML loads an unquoted port as a number. It still has to be substituted.
        (
            {
                "url": "https://api.example.com:{port}/{basePath}",
                "variables": {"port": {"default": 8443}, "basePath": {"default": "v2"}},
            },
            "https://api.example.com:8443/v2",
        ),
        # A variable with no default, or one the map does not declare, stays put
        # so the request fails visibly instead of dropping a path segment.
        (
            {"url": "https://api.example.com/{version}", "variables": {}},
            "https://api.example.com/{version}",
        ),
        (
            {
                "url": "https://api.example.com/{version}",
                "variables": {"version": {"enum": ["v1"]}},
            },
            "https://api.example.com/{version}",
        ),
        (
            {
                "url": "https://api.example.com/{version}",
                "variables": {"other": {"default": "v1"}},
            },
            "https://api.example.com/{version}",
        ),
        ({}, ""),
        ({"url": None}, ""),
    ],
)
def test_expand_openapi_server_url(server, expected):
    from mcp2cli import _expand_openapi_server_url

    assert _expand_openapi_server_url(server) == expected
