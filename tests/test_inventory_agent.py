"""The inventory agent: a small, content-free projection into Qdrant."""

from __future__ import annotations

import json
from pathlib import Path

from sparknet_loader import load_by_path

ROLE_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    return load_by_path(name, path)


agent = _load("sparknet_inventory_agent", ROLE_ROOT / "services" / "sparknet-inventory-agent.py")


def test_normalizes_a_user_without_secrets():
    row = agent.normalize_user({"id": "u-1", "email": "u@example.test", "role": "user", "status": "active"})
    assert row["kind"] == "user"
    assert row["user_id"] == "u-1"
    assert row["email"] == "u@example.test"
    assert "password" not in json.dumps(row)


def test_normalizes_a_model_and_keeps_the_family():
    row = agent.normalize_model({"id": "qwen2.5:7b", "base_model_id": "qwen2.5"}, "ollama")
    assert row["kind"] == "model"
    assert row["model_id"] == "qwen2.5:7b"
    assert row["family"] == "qwen2.5"
    assert row["source"] == "ollama"


def test_session_usage_reads_models_not_content(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "g1.json").write_text(
        json.dumps(
            {
                "model": "plan",
                "grant": {"user_identity": "u-1"},
                "turns": [
                    {"role": "user", "content": "SECRET PROMPT"},
                    {"role": "assistant", "content": "SECRET ANSWER"},
                ],
            }
        )
    )
    counts = agent.session_usage(tmp_path)
    assert counts[("u-1", "plan")] == 2
    rendered = json.dumps([list(key) for key in counts])
    assert "SECRET" not in rendered


def test_collect_dedupes_by_kind_and_object_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPARKNET_WEBUI_ADMIN_TOKEN", "token")

    def fake_http(url: str, token: str | None = None, timeout: float = 10.0):
        if url.endswith("/api/v1/users/"):
            return 200, {"users": [{"id": "u-1", "email": "u@example.test", "role": "user"}]}
        if url.endswith("/api/v1/models/all"):
            return 200, {"models": [{"id": "m-1"}]}
        if url.endswith("/api/tags"):
            return 200, {"models": [{"name": "m-1"}]}
        if url.endswith("/v1/models"):
            return 200, {"data": [{"id": "m-1"}]}
        return 0, None

    monkeypatch.setattr(agent, "_http_json", fake_http)
    rows = agent.collect(state=tmp_path / "state")
    keys = [(row["kind"], row["object_id"]) for row in rows]
    assert ("user", "u-1") in keys
    assert ("model", "m-1") in keys
    assert len(keys) == len(set(keys))
