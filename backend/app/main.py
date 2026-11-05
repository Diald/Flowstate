from __future__ import annotations

import json
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

DB_PATH = Path(os.getenv("WORKFLOW_DB", str(Path(__file__).resolve().parents[1] / "workflow.db")))
MAX_NODES = 50
MAX_OUTPUT_BYTES = 256_000
SUPPORTED = {"input", "mock_llm", "condition", "output"}
UNAVAILABLE = {"agent", "loop", "python", "http"}


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
          output_json TEXT, error_json TEXT, logs TEXT NOT NULL, duration_ms INTEGER NOT NULL
        );
        """)


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
    """Translate React Flow's historical `workflow` card type to runtime node types."""
    normalized = dict(graph)
    normalized["nodes"] = [
        {**node, "type": node.get("data", {}).get("kind")}
        if node.get("type") == "workflow" and node.get("data", {}).get("kind")
        else node
        for node in graph.get("nodes", [])
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
    for edge in edges:
        source, target = edge.get("source"), edge.get("target")
        if source not in node_ids or target not in node_ids:
            errors.append("Every edge must connect existing nodes.")
            continue
        adjacency[source].append(target)
        indegree[target] += 1
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
        errors.append("Cycles are not supported in v1. Use an explicit Loop node when available.")
    if not any(n.get("type") == "input" for n in nodes):
        errors.append("Add an Input node.")
    if not any(n.get("type") == "output" for n in nodes):
        errors.append("Add an Output node.")
    return errors


def resolve(value: Any, outputs: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, str):
        # Small, non-evaluating {{nodes.id.field}} template syntax.
        import re
        def replace(match: re.Match[str]) -> str:
            path = match.group(1).strip().split(".")
            if len(path) < 3 or path[0] != "nodes":
                return match.group(0)
            result: Any = outputs.get(path[1], {})
            for part in path[2:]:
                result = result.get(part) if isinstance(result, dict) else None
            return "" if result is None else str(result)
        return re.sub(r"{{\s*([^{}]+)\s*}}", replace, value)
    if isinstance(value, dict):
        return {k: resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs) for v in value]
    return value


def execute(graph: dict[str, Any], supplied: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    graph = normalize_graph(graph)
    nodes = graph["nodes"]
    edges = graph.get("edges", [])
    by_id = {n["id"]: n for n in nodes}
    incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        incoming[edge["target"]].append(edge)
        outgoing[edge["source"]].append(edge)
    indegree = {n["id"]: len(incoming[n["id"]]) for n in nodes}
    queue = deque(n["id"] for n in nodes if indegree[n["id"]] == 0)
    order = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for edge in outgoing[node_id]:
            indegree[edge["target"]] -= 1
            if indegree[edge["target"]] == 0:
                queue.append(edge["target"])
    outputs: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    final: Any = None
    for node_id in order:
        node = by_id[node_id]
        kind = node["type"]
        config = node.get("data", {}).get("config", {})
        node_inputs: dict[str, Any] = {}
        for edge in incoming[node_id]:
            source_output = outputs.get(edge["source"], {})
            handle = edge.get("sourceHandle") or "value"
            node_inputs[edge.get("targetHandle") or "input"] = source_output.get(handle)
        if kind == "input":
            result = {"value": supplied}
            node_inputs = {"supplied": supplied}
        elif kind == "mock_llm":
            template = config.get("prompt", "Mock response: {{nodes.input.value}}")
            rendered = resolve(template, outputs)
            # Make the mock behavior deterministic and transparent for demos.
            result = {"text": f"Mock response: {rendered}"}
            node_inputs = {"prompt": rendered}
        elif kind == "condition":
            left = node_inputs.get("input", supplied)
            expected = config.get("equals", True)
            passed = left == expected
            result = {"value": passed, "branch": "true" if passed else "false"}
        else:  # output
            value = config.get("value", "{{nodes.mock_llm.text}}")
            result = {"value": resolve(value, outputs)}
            if node_inputs.get("input") is not None and "value" not in config:
                result["value"] = node_inputs["input"]
            final = result["value"]
        outputs[node_id] = result
        record = {"node_id": node_id, "node_name": node.get("data", {}).get("label", kind), "status": "succeeded", "inputs": node_inputs, "outputs": result, "logs": [f"Executed {kind} node."], "duration_ms": 0}
        records.append(record)
    return {"value": final}, records


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/node-types")
def node_types() -> dict[str, Any]:
    return {"available": sorted(SUPPORTED), "unavailable": sorted(UNAVAILABLE)}


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
def run_workflow(workflow_id: str, body: RunBody) -> dict[str, Any]:
    workflow = get_workflow(workflow_id)
    graph = workflow["definition"]
    errors = validate_graph(graph)
    if errors:
        raise HTTPException(422, detail={"message": "Workflow validation failed", "errors": errors})
    run_id, started = str(uuid.uuid4()), now()
    try:
        result, records = execute(graph, body.input)
        if len(json.dumps(result)) > MAX_OUTPUT_BYTES:
            raise ValueError("Run output exceeds the size limit.")
        status, error = "succeeded", None
    except Exception as exc:
        result, records, status, error = None, [], "failed", {"message": str(exc)}
    completed = now()
    with connect() as db:
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, workflow_id, status, json.dumps(body.input), json.dumps(result), json.dumps(error), started, completed))
        for record in records:
            db.execute("INSERT INTO node_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(uuid.uuid4()), run_id, record["node_id"], record["node_name"], record["status"], json.dumps(record["inputs"]), json.dumps(record["outputs"]), None, json.dumps(record["logs"]), record["duration_ms"]))
    return {"id": run_id, "workflow_id": workflow_id, "status": status, "input": body.input, "output": result, "error": error, "started_at": started, "completed_at": completed, "nodes": records}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        nodes = db.execute("SELECT * FROM node_executions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
    if not row:
        raise HTTPException(404, "Run not found")
    return {"id": row["id"], "workflow_id": row["workflow_id"], "status": row["status"], "input": json.loads(row["input_json"]), "output": json.loads(row["output_json"]) if row["output_json"] else None, "error": json.loads(row["error_json"]) if row["error_json"] else None, "started_at": row["started_at"], "completed_at": row["completed_at"], "nodes": [{"node_id": n["node_id"], "node_name": n["node_name"], "status": n["status"], "inputs": json.loads(n["input_json"]), "outputs": json.loads(n["output_json"]) if n["output_json"] else None, "error": json.loads(n["error_json"]) if n["error_json"] else None, "logs": json.loads(n["logs"]), "duration_ms": n["duration_ms"]} for n in nodes]}
