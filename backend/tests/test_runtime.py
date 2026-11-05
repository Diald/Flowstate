from fastapi.testclient import TestClient
from app.main import app, validate_graph


def graph():
    return {"schema_version": "1.0", "nodes": [
        {"id": "input", "type": "input", "position": {"x": 0, "y": 0}, "data": {"label": "Input", "kind": "input", "config": {}}},
        {"id": "llm", "type": "mock_llm", "position": {"x": 200, "y": 0}, "data": {"label": "Mock LLM", "kind": "mock_llm", "config": {"prompt": "Hello {{nodes.input.value.name}}"}}},
        {"id": "output", "type": "output", "position": {"x": 400, "y": 0}, "data": {"label": "Output", "kind": "output", "config": {"value": "{{nodes.llm.text}}"}}}],
        "edges": [{"id": "a", "source": "input", "target": "llm"}, {"id": "b", "source": "llm", "target": "output"}]}


def test_validator_rejects_cycles_and_unavailable_nodes():
    definition = graph()
    definition["edges"].append({"id": "c", "source": "output", "target": "input"})
    assert any("Cycles" in e for e in validate_graph(definition))
    definition = graph()
    definition["nodes"][1]["type"] = "python"
    assert any("unavailable" in e for e in validate_graph(definition))


def test_workflow_crud_and_run(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "test.db")
    with TestClient(app) as client:
        created = client.post("/api/workflows", json={"name": "Demo", "description": "", "definition": graph()}).json()
        assert client.get(f"/api/workflows/{created['id']}").status_code == 200
        run = client.post(f"/api/workflows/{created['id']}/runs", json={"input": {"name": "Ada"}}).json()
        assert run["status"] == "succeeded"
        assert run["output"]["value"] == "Mock response: Hello Ada"
        inspected = client.get(f"/api/runs/{run['id']}").json()
        assert len(inspected["nodes"]) == 3
        assert client.delete(f"/api/workflows/{created['id']}").json()["deleted"]


def test_local_frontend_cors_preflight():
    with TestClient(app) as client:
        response = client.options(
            "/api/workflows",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_legacy_react_flow_workflow_type_can_run(monkeypatch, tmp_path):
    monkeypatch.setattr("app.main.DB_PATH", tmp_path / "legacy.db")
    legacy = graph()
    for node in legacy["nodes"]:
        node["type"] = "workflow"
    with TestClient(app) as client:
        created = client.post("/api/workflows", json={"name": "Legacy", "description": "", "definition": legacy}).json()
        valid = client.post(f"/api/workflows/{created['id']}/validate").json()
        assert valid["valid"]
        run = client.post(f"/api/workflows/{created['id']}/runs", json={"input": {"name": "Ada"}}).json()
        assert run["status"] == "succeeded"
        assert run["output"]["value"] == "Mock response: Hello Ada"
