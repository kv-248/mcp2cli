"""Automatic GraphQL selections must not request fields that need arguments.

A field such as ``repositories(first: Int!)`` or ``avatar(size: Int!)`` cannot
be selected without that argument. GraphQL rejects the whole operation, so a
query whose caller only wanted ``id`` fails. Fields stay in the selection when
every non-null argument has a default, and when an argument is optional even
if its items are non-null (``tags: [String!]``).
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import mcp2cli


def _scalar(name):
    return {"kind": "SCALAR", "name": name, "ofType": None}


def _non_null(inner):
    return {"kind": "NON_NULL", "name": None, "ofType": inner}


def _list(inner):
    return {"kind": "LIST", "name": None, "ofType": inner}


def _field(name, type_ref, args=None):
    return {"name": name, "args": args or [], "type": type_ref}


def _argument(name, type_ref, default=None):
    return {"name": name, "type": type_ref, "defaultValue": default}


class TestSelectionSkipsRequiredArguments:
    def _types(self):
        return {
            "User": {
                "kind": "OBJECT",
                "name": "User",
                "fields": [
                    _field("id", _non_null(_scalar("ID"))),
                    _field(
                        "avatar",
                        _scalar("String"),
                        [_argument("size", _non_null(_scalar("Int")))],
                    ),
                    _field(
                        "repositories",
                        {"kind": "OBJECT", "name": "RepositoryConnection", "ofType": None},
                        [_argument("first", _non_null(_scalar("Int")))],
                    ),
                    _field(
                        "bio",
                        _scalar("String"),
                        [_argument("format", _non_null(_scalar("String")), default='"md"')],
                    ),
                    _field(
                        "website",
                        _scalar("String"),
                        [_argument("lang", _scalar("String"))],
                    ),
                    # Optional list argument whose items are non-null. The
                    # argument itself may be omitted, so the field is selectable.
                    _field(
                        "tags",
                        _scalar("String"),
                        [_argument("values", _list(_non_null(_scalar("String"))))],
                    ),
                    # The list argument itself is non-null, so the field is not.
                    _field(
                        "labels",
                        _scalar("String"),
                        [_argument("values", _non_null(_list(_non_null(_scalar("String")))))],
                    ),
                ],
            },
            "RepositoryConnection": {
                "kind": "OBJECT",
                "name": "RepositoryConnection",
                "fields": [_field("totalCount", _scalar("Int"))],
            },
        }

    def test_required_arguments_are_left_out(self):
        selection = mcp2cli._build_selection_set(
            {"kind": "OBJECT", "name": "User", "ofType": None},
            self._types(),
        )
        names = selection.split()
        assert "id" in names
        assert "bio" in names
        assert "website" in names
        assert "tags" in names
        assert "avatar" not in selection
        assert "repositories" not in selection
        assert "labels" not in selection
        assert "totalCount" not in selection

    def test_optional_argument_keeps_a_nested_object(self):
        types = self._types()
        types["User"]["fields"].append(
            _field(
                "profile",
                {"kind": "OBJECT", "name": "RepositoryConnection", "ofType": None},
                [_argument("verbose", _scalar("Boolean"))],
            )
        )
        selection = mcp2cli._build_selection_set(
            {"kind": "OBJECT", "name": "User", "ofType": None},
            types,
        )
        assert "profile { totalCount }" in selection


# ``avatar`` and ``repositories`` require arguments. ``bio``'s argument has a
# default, so a client may select it without passing one.
_SCHEMA = {
    "queryType": {"name": "Query"},
    "mutationType": None,
    "types": [
        {
            "kind": "OBJECT",
            "name": "Query",
            "fields": [
                _field("viewer", {"kind": "OBJECT", "name": "User", "ofType": None})
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "OBJECT",
            "name": "User",
            "fields": [
                _field("id", _non_null(_scalar("ID"))),
                _field(
                    "avatar",
                    _scalar("String"),
                    [_argument("size", _non_null(_scalar("Int")))],
                ),
                _field(
                    "repositories",
                    {"kind": "OBJECT", "name": "RepositoryConnection", "ofType": None},
                    [_argument("first", _non_null(_scalar("Int")))],
                ),
                _field(
                    "bio",
                    _scalar("String"),
                    [_argument("format", _non_null(_scalar("String")), default='"md"')],
                ),
            ],
            "inputFields": None,
            "enumValues": None,
        },
        {
            "kind": "OBJECT",
            "name": "RepositoryConnection",
            "fields": [_field("totalCount", _scalar("Int"))],
            "inputFields": None,
            "enumValues": None,
        },
        {"kind": "SCALAR", "name": "ID", "fields": None, "inputFields": None, "enumValues": None},
        {"kind": "SCALAR", "name": "String", "fields": None, "inputFields": None, "enumValues": None},
        {"kind": "SCALAR", "name": "Int", "fields": None, "inputFields": None, "enumValues": None},
    ],
}


class _StrictGraphQLHandler(BaseHTTPRequestHandler):
    """Rejects a selection that omits a required field argument."""

    last_query = ""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        query = body.get("query") or ""
        _StrictGraphQLHandler.last_query = query
        if "__schema" in query:
            payload = {"data": {"__schema": _SCHEMA}}
        elif "avatar" in query or "repositories" in query:
            payload = {
                "errors": [
                    {"message": "Field argument is required but it was not provided"}
                ]
            }
        else:
            payload = {"data": {"viewer": {"id": "1", "bio": "hi"}}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture
def strict_graphql_server():
    _StrictGraphQLHandler.last_query = ""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StrictGraphQLHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_viewer_query_omits_fields_that_require_arguments(tmp_path, strict_graphql_server):
    env = os.environ.copy()
    env["MCP2CLI_CACHE_DIR"] = str(tmp_path / "cache")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mcp2cli",
            "--graphql",
            strict_graphql_server,
            "--json",
            "viewer",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"id": "1", "bio": "hi"}
    query = _StrictGraphQLHandler.last_query
    assert "avatar" not in query
    assert "repositories" not in query
    assert "id" in query
    assert "bio" in query
