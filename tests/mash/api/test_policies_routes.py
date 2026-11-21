"""PUT and GET /api/v1/policies/{policy_id}."""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from mash.api import create_app
from mash.api.config import MashHostConfig
from mash.runtime import HostBuilder


@contextmanager
def _client():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(
        os.environ, {"MASH_DATA_DIR": str(Path(tmp)), "MASH_DATABASE_URL": ""}
    ):
        pool = HostBuilder().build()
        app = create_app(
            pool, config=MashHostConfig(runtime_database_url="postgresql://test/runtime")
        )
        with TestClient(app) as client:
            yield client


def test_an_empty_body_creates_the_default_policy() -> None:
    with _client() as client:
        resp = client.put("/api/v1/policies/acme-ws", json={})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert {k: data[k] for k in ("policy_id", "sandbox", "browser_sites", "wait_max_seconds")} == {
            "policy_id": "acme-ws",
            "sandbox": True,
            "browser_sites": None,
            "wait_max_seconds": 86400,
        }
        assert data["created_at"] == data["updated_at"]

        got = client.get("/api/v1/policies/acme-ws")
        assert got.status_code == 200
        assert got.json()["data"] == data


def test_put_replaces_the_whole_policy() -> None:
    with _client() as client:
        first = client.put(
            "/api/v1/policies/p-2",
            json={"sandbox": False, "browser_sites": ["https://*.pge.com"], "wait_max_seconds": None},
        ).json()["data"]
        assert (first["sandbox"], first["browser_sites"], first["wait_max_seconds"]) == (
            False,
            ["https://*.pge.com"],
            None,
        )

        second = client.put("/api/v1/policies/p-2", json={"wait_max_seconds": 600}).json()["data"]
        assert (second["sandbox"], second["browser_sites"], second["wait_max_seconds"]) == (
            True,
            None,
            600,
        )
        assert second["created_at"] == first["created_at"]
        assert second["updated_at"] >= first["updated_at"]


def test_an_unknown_policy_is_404() -> None:
    with _client() as client:
        resp = client.get("/api/v1/policies/nope")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "POLICY_NOT_FOUND"


def test_a_bad_policy_id_is_400() -> None:
    with _client() as client:
        resp = client.put("/api/v1/policies/has%20space", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"
        resp = client.get(f"/api/v1/policies/{'x' * 256}")
        assert resp.status_code == 400


def test_a_bad_policy_fails_validation_and_stores_nothing() -> None:
    with _client() as client:
        for body in (
            {"wait_max_seconds": 59},
            {"browser_sites": ["pge.com"]},
            {"browser_sites": ["https://pge.com/login"]},
            {"sandbox": "maybe"},
            {"memory": "app"},
        ):
            resp = client.put("/api/v1/policies/p-new", json=body)
            assert resp.status_code == 422, body
        assert client.get("/api/v1/policies/p-new").status_code == 404

        resp = client.put("/api/v1/policies/p-new", json={"browser_sites": ["pge.com"]})
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        [error] = resp.json()["error"]["details"]["errors"]
        assert "must be an http(s) origin" in error["msg"]


def test_an_empty_site_list_is_kept() -> None:
    with _client() as client:
        data = client.put("/api/v1/policies/p-1", json={"browser_sites": []}).json()["data"]
        assert data["browser_sites"] == []
