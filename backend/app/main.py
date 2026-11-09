from __future__ import annotations

import json
import re
import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .providers.registry import providers

DB_PATH = Path(os.getenv("WORKFLOW_DB", str(Path(__file__).resolve().parents[1] / "workflow.db")))
MAX_NODES = 50
MAX_OUTPUT_BYTES = 256_000
SUPPORTED = {"input", "mock_llm", "llm", "condition", "loop", "output"}
UNAVAILABLE = {"agent", "python", "http"}
MAX_LOOP_ITERATIONS = 20
MAX_OUTPUT_TOKENS = 8192


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS workflows (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          definition TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, status TEXT NOT NULL,
          input_json TEXT NOT NULL, output_json TEXT, error_json TEXT,
          started_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS node_executions (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
          node_name TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL,
          output_json TEXT, error_json TEXT, logs TEXT NOT NULL, duration_ms INTEGER NOT NULL,
          provider_metadata_json TEXT
        );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(node_executions)").fetchall()}
        if "provider_metadata_json" not in columns:
            db.execute("ALTER TABLE node_executions ADD COLUMN provider_metadata_json TEXT")


app = FastAPI(title="Visual Workflow API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup() -> None:
    init_db()


class WorkflowBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    definition: dict[str, Any]


class RunBody(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Normalize legacy React Flow card/handle defaults to the runtime contract."""
    normalized = dict(graph)
    normalized_nodes = [
        {**node, "type": node.get("data", {}).get("kind")}
        if node.get("type") == "workflow" and node.get("data", {}).get("kind")
        else node
        for node in graph.get("nodes", [])
    ]
    node_by_id = {node.get("id"): node for node in normalized_nodes}
    default_outputs = {"input": "value", "mock_llm": "text", "llm": "text", "loop": "value"}
    normalized["nodes"] = normalized_nodes
    normalized["edges"] = [
        {
            **edge,
            "sourceHandle": edge.get("sourceHandle") or default_outputs.get(node_by_id.get(edge.get("source"), {}).get("type")),
            "targetHandle": edge.get("targetHandle") or "input",
        }
        for edge in graph.get("edges", [])
    ]
    return normalized


def validate_graph(graph: dict[str, Any]) -> list[str]:
    graph = normalize_graph(graph)
    errors: list[str] = []
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        return ["Add at least one node."]
    if len(nodes) > MAX_NODES:
        errors.append(f"A workflow may contain at most {MAX_NODES} nodes.")
    ids = [str(n.get("id", "")) for n in nodes]
    if len(ids) != len(set(ids)):
        errors.append("Node IDs must be unique.")
    node_ids = set(ids)
    indegree = {node_id: 0 for node_id in node_ids}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        kind = node.get("type")
        if kind not in SUPPORTED:
            label = "unavailable" if kind in UNAVAILABLE else "unknown"
            errors.append(f"Node '{node.get('data', {}).get('label', node.get('id'))}' has {label} type '{kind}'.")
    node_by_id = {str(node.get("id")): node for node in nodes}
    valid_handles = {
        "input": {"value"}, "mock_llm": {"text"}, "llm": {"text"},
        "condition": {"true", "false"}, "loop": {"value"}, "output": set(),
    }
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if source not in node_ids or target not in node_ids:
            errors.append("Every edge must connect existing nodes.")
            continue
        source_type = node_by_id[source].get("type")
        source_handle = edge.get("sourceHandle")
        if source_handle and source_type in valid_handles and source_handle not in valid_handles[source_type]:
            errors.append(f"Edge from '{source}' uses invalid output '{source_handle}'.")
        if source_type == "condition" and source_handle not in {"true", "false"}:
            errors.append(f"Condition node '{source}' connections must use the true or false output.")
        adjacency[source].append(target)
        indegree[target] += 1
    for node in nodes:
        kind = node.get("type")
        config = node.get("data", {}).get("config", {})
        if kind == "llm" and config.get("provider", "gemini") not in providers.names():
            errors.append(f"Node '{node.get('data', {}).get('label', node.get('id'))}' uses an unknown provider.")
        if kind == "condition" and config.get("operator", "eq") not in {"eq", "neq", "gt", "gte", "lt", "lte", "truthy", "falsy", "exists"}:
            errors.append(f"Condition node '{node.get('id')}' uses an unsupported comparison.")
        if kind == "loop":
            limit = config.get("max_iterations", 3)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > MAX_LOOP_ITERATIONS:
                errors.append(f"Loop node '{node.get('id')}' must set max_iterations between 1 and {MAX_LOOP_ITERATIONS}.")
    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    seen = 0
    while queue:
        current = queue.popleft()
        seen += 1
        for target in adjacency[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if seen != len(node_ids):
        errors.append("Cycles are not supported. Use the bounded Loop node to process a collection.")
    if not any(n.get("type") == "input" for n in nodes):
        errors.append("Add an Input node.")
    if not any(n.get("type") == "output" for n in nodes):
        errors.append("Add an Output node.")
    return errors


_TEMPLATE = re.compile(r"{{\s*([^{}]+)\s*}}")


def resolve(value: Any, outputs: dict[str, dict[str, Any]], extra: dict[str, Any] | None = None) -> Any:
    if isinstance(value, str):
        matches = list(_TEMPLATE.finditer(value))
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return _resolve_path(matches[0].group(1).strip(), outputs, extra)

        def replace(match: re.Match[str]) -> str:
            result = _resolve_path(match.group(1).strip(), outputs, extra)
            if result is None:
                return ""
            if isinstance(result, (dict, list)):
                return json.dumps(result, ensure_ascii=False)
            return str(result)
        return _TEMPLATE.sub(replace, value)
    if isinstance(value, dict):
        return {k: resolve(v, outputs, extra) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs, extra) for v in value]
    return value


def _resolve_path(path: str, outputs: dict[str, dict[str, Any]], extra: dict[str, Any] | None) -> Any:
    parts = path.split(".")
    if parts[0] in (extra or {}):
        result: Any = (extra or {})[parts[0]]
        parts = parts[1:]
    elif len(parts) >= 3 and parts[0] == "nodes":
        result = outputs.get(parts[1], {})
        parts = parts[2:]
    else:
        return "{{" + path + "}}"
    for part in parts:
        if isinstance(result, dict):
            result = result.get(part)
        elif isinstance(result, list) and part.isdigit() and int(part) < len(result):
            result = result[int(part)]
        else:
            return None
    return result


def _condition_passes(value: Any, config: dict[str, Any]) -> bool:
    operator = config.get("operator", "eq")
    expected = config.get("value", config.get("equals", True))
    path = config.get("path", "")
    if path:
        for segment in str(path).split("."):
            value = value.get(segment) if isinstance(value, dict) else None
    if operator == "exists":
        return value is not None
    if operator == "truthy":
        return bool(value)
    if operator == "falsy":
        return not bool(value)
    if operator == "eq":
        return value == expected
    if operator == "neq":
        return value != expected
    if operator in {"gt", "gte", "lt", "lte"}:
        try:
            left, right = float(value), float(expected)
        except (TypeError, ValueError):
            return False
        return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[operator]
    return False


async def execute(graph: dict[str, Any], supplied: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    graph = normalize_graph(graph)
    nodes = graph["nodes"]
    edges = graph.get("edges", [])
    by_id = {n["id"]: n for n in nodes}
    incoming: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    outgoing: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, edge in enumerate(edges):
        incoming[edge["target"]].append((index, edge))
        outgoing[edge["source"]].append((index, edge))
    indegree = {n["id"]: len(incoming[n["id"]]) for n in nodes}
    queue = deque(n["id"] for n in nodes if indegree[n["id"]] == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for _, edge in outgoing[node_id]:
            indegree[edge["target"]] -= 1
            if indegree[edge["target"]] == 0:
                queue.append(edge["target"])

    outputs: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    active_edges: set[int] = set()
    executed_nodes: set[str] = set()
    final: Any = None
    for node_id in order:
        node = by_id[node_id]
        kind = node["type"]
        config = node.get("data", {}).get("config", {})
        live_inputs = [(idx, edge) for idx, edge in incoming[node_id] if idx in active_edges]
        if incoming[node_id] and not live_inputs:
            records.append({"node_id": node_id, "node_name": node.get("data", {}).get("label", kind), "status": "skipped", "inputs": {}, "outputs": {}, "logs": ["Skipped because no active branch reached this node."], "duration_ms": 0})
            continue
        executed_nodes.add(node_id)
        node_inputs: dict[str, Any] = {}
        for _, edge in live_inputs:
            source_output = outputs.get(edge["source"], {})
            handle = edge.get("sourceHandle") or "value"
            node_inputs[edge.get("targetHandle") or "input"] = source_output.get(handle)
        logs: list[Any] = [f"Executed {kind} node."]
        metadata: dict[str, Any] = {}
        if kind == "input":
            result = {"value": supplied}
            node_inputs = {"supplied": supplied}
        elif kind == "mock_llm":
            template = config.get("prompt", "Mock response: {{nodes.input.value}}")
            rendered = resolve(template, outputs)
            result = {"text": f"Mock response: {rendered}"}
            node_inputs = {"prompt": rendered}
        elif kind == "llm":
            prompt = resolve(config.get("prompt", ""), outputs)
            system_prompt = resolve(config.get("system_prompt", ""), outputs)
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"LLM node '{node_id}' needs a prompt template.")
            provider_name = config.get("provider", "gemini")
            model = config.get("model", "gemini-3.8-flash")
            temperature = min(2.0, max(0.0, float(config.get("temperature", 1.0))))
            max_tokens = min(MAX_OUTPUT_TOKENS, max(1, int(config.get("max_output_tokens", 1024))))
            timeout = min(120.0, max(1.0, float(config.get("timeout_seconds", 30))))
            generated = await providers.get(provider_name).generate(
                prompt=prompt, system_prompt=str(system_prompt), model=model,
                temperature=temperature, max_output_tokens=max_tokens, timeout_seconds=timeout,
            )
            result = {"text": generated.text, "provider": generated.provider, "model": generated.model}
            node_inputs = {"prompt": prompt, "system_prompt": system_prompt}
            metadata = {"provider": generated.provider, "model": generated.model, "latency_ms": generated.latency_ms,
                        "input_tokens": generated.input_tokens, "output_tokens": generated.output_tokens}
        elif kind == "condition":
            condition_input = node_inputs.get("input", supplied)
            passed = _condition_passes(condition_input, config)
            branch = "true" if passed else "false"
            result = {"value": passed, "branch": branch, "true": condition_input if passed else None, "false": condition_input if not passed else None}
            logs = [f"Condition evaluated to {branch} using '{config.get('operator', 'eq')}'."]
        elif kind == "loop":
            collection = resolve(config.get("collection", "{{nodes.input.value.items}}"), outputs)
            if not isinstance(collection, list):
                raise ValueError(f"Loop node '{node_id}' collection must resolve to a list.")
            limit = min(MAX_LOOP_ITERATIONS, int(config.get("max_iterations", 3)))
            selected = collection[:limit]
            item_template = config.get("item_template", "{{item}}")
            values = []
            iterations = []
            for iteration, item in enumerate(selected):
                rendered = resolve(item_template, outputs, {"item": item, "index": iteration})
                values.append(rendered)
                iterations.append({"iteration": iteration + 1, "input": item, "output": rendered})
            stop_reason = "max_iterations" if len(collection) > limit else "collection_exhausted"
            result = {"value": values, "items": values, "iteration_count": len(values), "stop_reason": stop_reason}
            node_inputs = {"collection": collection}
            logs = iterations + [{"stop_reason": stop_reason}]
        else:  # output
            value_template = config.get("value")
            if isinstance(value_template, str) and value_template.strip():
                result = {"value": resolve(value_template, outputs)}
            else:
                result = {"value": node_inputs.get("input")}
            final = result["value"]
        outputs[node_id] = result
        records.append({"node_id": node_id, "node_name": node.get("data", {}).get("label", kind), "status": "succeeded", "inputs": node_inputs, "outputs": result, "logs": logs, "duration_ms": metadata.get("latency_ms", 0), "provider_metadata": metadata or None})
        for edge_index, edge in outgoing[node_id]:
            if kind == "condition" and edge.get("sourceHandle") != result["branch"]:
                continue
            active_edges.add(edge_index)
    return {"value": final}, records


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/node-types")
def node_types() -> dict[str, Any]:
    return {"available": sorted(SUPPORTED), "unavailable": sorted(UNAVAILABLE), "providers": ["mock", *providers.names()]}


@app.get("/api/workflows")
def list_workflows() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM workflows ORDER BY updated_at DESC").fetchall()
    return [{"id": r["id"], "name": r["name"], "description": r["description"], "definition": json.loads(r["definition"]), "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


@app.post("/api/workflows")
def create_workflow(body: WorkflowBody) -> dict[str, Any]:
    workflow_id, stamp = str(uuid.uuid4()), now()
    with connect() as db:
        db.execute("INSERT INTO workflows VALUES (?, ?, ?, ?, ?, ?)", (workflow_id, body.name, body.description, json.dumps(body.definition), stamp, stamp))
    return {"id": workflow_id, "name": body.name, "description": body.description, "definition": body.definition, "created_at": stamp, "updated_at": stamp}


@app.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Workflow not found")
    return {"id": row["id"], "name": row["name"], "description": row["description"], "definition": json.loads(row["definition"]), "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.patch("/api/workflows/{workflow_id}")
def update_workflow(workflow_id: str, body: WorkflowBody) -> dict[str, Any]:
    stamp = now()
    with connect() as db:
        result = db.execute("UPDATE workflows SET name=?, description=?, definition=?, updated_at=? WHERE id=?", (body.name, body.description, json.dumps(body.definition), stamp, workflow_id))
        if not result.rowcount:
            raise HTTPException(404, "Workflow not found")
    return get_workflow(workflow_id)


@app.delete("/api/workflows/{workflow_id}")
def delete_workflow(workflow_id: str) -> dict[str, bool]:
    with connect() as db:
        result = db.execute("DELETE FROM workflows WHERE id=?", (workflow_id,))
        if not result.rowcount:
            raise HTTPException(404, "Workflow not found")
    return {"deleted": True}


@app.post("/api/workflows/{workflow_id}/duplicate")
def duplicate_workflow(workflow_id: str) -> dict[str, Any]:
    original = get_workflow(workflow_id)
    body = WorkflowBody(name=f"{original['name']} copy", description=original["description"], definition=original["definition"])
    return create_workflow(body)


@app.post("/api/workflows/{workflow_id}/validate")
def validate_workflow(workflow_id: str) -> dict[str, Any]:
    workflow = get_workflow(workflow_id)
    errors = validate_graph(workflow["definition"])
    return {"valid": not errors, "errors": errors}


@app.post("/api/workflows/{workflow_id}/runs")
async def run_workflow(workflow_id: str, body: RunBody) -> dict[str, Any]:
    workflow = get_workflow(workflow_id)
    graph = workflow["definition"]
    errors = validate_graph(graph)
    if errors:
        raise HTTPException(422, detail={"message": "Workflow validation failed", "errors": errors})
    run_id, started = str(uuid.uuid4()), now()
    try:
        result, records = await execute(graph, body.input)
        if len(json.dumps(result)) > MAX_OUTPUT_BYTES:
            raise ValueError("Run output exceeds the size limit.")
        status, error = "succeeded", None
    except Exception as exc:
        result, records, status, error = None, [], "failed", {"message": str(exc)}
    completed = now()
    with connect() as db:
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, workflow_id, status, json.dumps(body.input), json.dumps(result), json.dumps(error), started, completed))
        for record in records:
            db.execute(
                "INSERT INTO node_executions (id, run_id, node_id, node_name, status, input_json, output_json, error_json, logs, duration_ms, provider_metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), run_id, record["node_id"], record["node_name"], record["status"], json.dumps(record["inputs"]), json.dumps(record["outputs"]), None, json.dumps(record["logs"]), record["duration_ms"], json.dumps(record.get("provider_metadata")) if record.get("provider_metadata") else None),
            )
    return {"id": run_id, "workflow_id": workflow_id, "status": status, "input": body.input, "output": result, "error": error, "started_at": started, "completed_at": completed, "nodes": records}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        nodes = db.execute("SELECT * FROM node_executions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    if not row:
        raise HTTPException(404, "Run not found")
    return {"id": row["id"], "workflow_id": row["workflow_id"], "status": row["status"], "input": json.loads(row["input_json"]), "output": json.loads(row["output_json"]) if row["output_json"] else None, "error": json.loads(row["error_json"]) if row["error_json"] else None, "started_at": row["started_at"], "completed_at": row["completed_at"], "nodes": [{"node_id": n["node_id"], "node_name": n["node_name"], "status": n["status"], "inputs": json.loads(n["input_json"]), "outputs": json.loads(n["output_json"]) if n["output_json"] else None, "error": json.loads(n["error_json"]) if n["error_json"] else None, "logs": json.loads(n["logs"]), "duration_ms": n["duration_ms"], "provider_metadata": json.loads(n["provider_metadata_json"]) if n["provider_metadata_json"] else None} for n in nodes]}
