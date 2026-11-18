"""Generate a Postman collection (v2.1) from Prism's OpenAPI schema.

Usage:
    uv run python scripts/generate_postman.py            # write the collection
    uv run python scripts/generate_postman.py --check    # fail if it is out of date
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from prism import __version__
from prism.main import create_app

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "docs" / "postman" / "prism.postman_collection.json"
SCHEMA_URL = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
NAMESPACE = uuid.UUID("6f1c2a52-3d9e-4c8b-9a51-0b7f6d2e4a10")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
DEFAULT_FOLDER = "General"

JSONDict = dict[str, Any]


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


def _resolve(schema: JSONDict, spec: JSONDict) -> JSONDict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    node: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _example(schema: JSONDict, spec: JSONDict, depth: int = 0) -> Any:
    if depth > 8:
        return None
    schema = _resolve(schema, spec)
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if schema.get("examples"):
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if _resolve(s, spec).get("type") != "null"]
            return _example(options[0], spec, depth + 1) if options else None
    if "allOf" in schema:
        merged: JSONDict = {}
        for part in schema["allOf"]:
            value = _example(part, spec, depth + 1)
            if isinstance(value, dict):
                merged.update(value)
        return merged

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            name: _example(prop, spec, depth + 1)
            for name, prop in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [_example(schema.get("items", {}), spec, depth + 1)]
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    if kind == "string":
        return {"date-time": "2026-01-01T00:00:00Z", "uuid": str(uuid.UUID(int=0))}.get(
            schema.get("format", ""), "string"
        )
    return None


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _url(path: str, parameters: list[JSONDict], spec: JSONDict) -> JSONDict:
    postman_path = re.sub(r"\{([^}]+)\}", r":\1", path)
    url: JSONDict = {
        "raw": "{{baseUrl}}" + postman_path,
        "host": ["{{baseUrl}}"],
        "path": [segment for segment in postman_path.strip("/").split("/") if segment],
    }
    query: list[JSONDict] = []
    variables: list[JSONDict] = []
    for raw in parameters:
        param = _resolve(raw, spec)
        value = _example(param.get("schema", {}), spec)
        entry: JSONDict = {
            "key": param["name"],
            "value": _as_text(value),
            "description": param.get("description", ""),
        }
        if param.get("in") == "query":
            entry["disabled"] = not param.get("required", False)
            query.append(entry)
        elif param.get("in") == "path":
            variables.append(entry)
    if query:
        url["query"] = query
        url["raw"] += "?" + "&".join(f"{q['key']}={q['value']}" for q in query if not q["disabled"])
        url["raw"] = url["raw"].rstrip("?")
    if variables:
        url["variable"] = variables
    return url


def _request(path: str, method: str, operation: JSONDict, spec: JSONDict) -> JSONDict:
    parameters = [*spec["paths"][path].get("parameters", []), *operation.get("parameters", [])]
    headers: list[JSONDict] = []
    for raw in parameters:
        param = _resolve(raw, spec)
        if param.get("in") == "header":
            value = _example(param.get("schema", {}), spec)
            headers.append({"key": param["name"], "value": _as_text(value)})

    request: JSONDict = {
        "method": method.upper(),
        "header": headers,
        "url": _url(path, parameters, spec),
        "description": operation.get("description", ""),
    }

    body = operation.get("requestBody", {}).get("content", {}).get("application/json")
    if body is not None:
        headers.append({"key": "Content-Type", "value": "application/json"})
        request["body"] = {
            "mode": "raw",
            "raw": json.dumps(_example(body.get("schema", {}), spec), indent=2),
            "options": {"raw": {"language": "json"}},
        }

    if not operation.get("security", spec.get("security")):
        request["auth"] = {"type": "noauth"}
    return request


def build_collection(spec: JSONDict) -> JSONDict:
    folders: dict[str, list[JSONDict]] = {}
    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            folder = (operation.get("tags") or [DEFAULT_FOLDER])[0]
            name = operation.get("summary") or f"{method.upper()} {path}"
            folders.setdefault(folder, []).append(
                {
                    "id": _stable_id(method, path),
                    "name": name,
                    "request": _request(path, method, operation, spec),
                    "response": [],
                }
            )

    info = spec.get("info", {})
    return {
        "info": {
            "_postman_id": _stable_id("collection", info.get("title", "Prism")),
            "name": info.get("title", "Prism"),
            "description": f"Generated from the Prism {__version__} OpenAPI schema. "
            "Do not edit by hand: run `make postman`.",
            "schema": SCHEMA_URL,
        },
        "item": [
            {"id": _stable_id("folder", folder), "name": folder, "item": items}
            for folder, items in sorted(folders.items())
        ],
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{virtualKey}}", "type": "string"}],
        },
        "variable": [
            {"key": "baseUrl", "value": "http://127.0.0.1:8000", "type": "string"},
            {"key": "virtualKey", "value": "", "type": "string"},
        ],
    }


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def render() -> str:
    return json.dumps(build_collection(create_app().openapi()), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check", action="store_true", help="exit with 1 if the file is missing or out of date"
    )
    args = parser.parse_args(argv)
    content = render()
    output: Path = args.output

    if args.check:
        if not output.exists() or output.read_text(encoding="utf-8") != content:
            print(
                f"Postman collection is out of date: {_display(output)}\n"
                "Run `make postman` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("Postman collection is up to date.")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"Wrote {_display(output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
