"""Acceptance tests for the operator-only RWA mutation boundary."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest

from src.config import settings
from src.credit_manager import CreditManager
from src.resource_server import app, _store_rwa_observation_without_blocking
from src.rwa_adapters import RWAAdapterRegistry
from src.rwa_store import RWAObservationStore
from src import rwa_sourcing_runner
from src.rwa_security import operator_token_is_strong


OPERATOR_TOKEN = "rwa-test-operator-token-0123456789abcdef"
AUTHORIZATION = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rwa_client(monkeypatch, tmp_path):
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("RWA_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("RWA_OBSERVATION_DB_PATH", str(tmp_path / "rwa-evidence.db"))
    with TestClient(app) as client:
        yield client


def _observation_payload() -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    observation = {
        "symbol": "EUR/USD",
        "venue": "gains",
        "asset_class": "fx",
        "source_type": "price_stream_no_book",
        "value": 1.14,
        "timestamp": timestamp,
    }
    return {
        "raw_payload": dict(observation),
        "normalized_observation": dict(observation),
        "metadata": {"product": "operator_test"},
    }


def test_mutation_fails_closed_when_operator_token_is_unset(rwa_client, monkeypatch):
    monkeypatch.delenv("RWA_OPERATOR_TOKEN")

    response = rwa_client.post("/v1/rwa/observations/store", json=_observation_payload())

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "RWA_OPERATOR_AUTH_NOT_CONFIGURED"


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("/v1/rwa/observations/store", {}),
        ("/v1/rwa/observations/store", {"Authorization": "Bearer wrong-token"}),
        (f"/v1/rwa/observations/store?token={OPERATOR_TOKEN}", {}),
        ("/v1/rwa/observations/store", {"Cookie": f"rwa_operator={OPERATOR_TOKEN}"}),
    ],
)
def test_mutation_rejects_missing_wrong_query_and_cookie_credentials(
    rwa_client,
    url,
    headers,
):
    response = rwa_client.post(url, headers=headers, json=_observation_payload())

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "RWA_OPERATOR_AUTH_REQUIRED"


def test_mutation_feature_flag_fails_closed(rwa_client, monkeypatch):
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "false")

    response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        json=_observation_payload(),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["error_code"] == "RWA_MUTATIONS_DISABLED"


@pytest.mark.parametrize(
    "token",
    ["a" * 32, "changeme" * 4, "test" * 8, "replace-me-with-a-secret-token-value"],
)
def test_operator_token_strength_rejects_repeated_or_placeholder_values(token):
    assert operator_token_is_strong(token) is False


def test_probe_numeric_environment_falls_back_without_import_crash(monkeypatch):
    monkeypatch.setenv("RWA_PROBE_MAX_CONCURRENCY", "not-an-int")
    monkeypatch.setenv("RWA_PROBE_CALL_TIMEOUT_SECONDS", "NaN")
    monkeypatch.setenv("RWA_PROBE_TOTAL_TIMEOUT_SECONDS", "not-a-float")

    assert rwa_sourcing_runner._bounded_int_env(
        "RWA_PROBE_MAX_CONCURRENCY", 2, 1, 8
    ) == 2
    assert rwa_sourcing_runner._bounded_float_env(
        "RWA_PROBE_CALL_TIMEOUT_SECONDS", 10.0, 0.1, 30.0
    ) == 10.0
    assert rwa_sourcing_runner._bounded_float_env(
        "RWA_PROBE_TOTAL_TIMEOUT_SECONDS", 30.0, 0.1, 60.0
    ) == 30.0


def test_rwa_safeguard_configuration_loads_from_dotenv(tmp_path):
    evidence_path = tmp_path / "dotenv-rwa.db"
    (tmp_path / ".env").write_text(
        "\n".join(
            (
                "BLOCKSIZE_API_KEY=dotenv-test-key",
                "RWA_MUTATIONS_ENABLED=true",
                f"RWA_OPERATOR_TOKEN={OPERATOR_TOKEN}",
                f"RWA_OBSERVATION_DB_PATH={evidence_path}",
                "RWA_STORE_LOCK_TIMEOUT_SECONDS=0.75",
                "RWA_PROBE_CALL_TIMEOUT_SECONDS=7.5",
                "RWA_PROBE_TOTAL_TIMEOUT_SECONDS=22.5",
                "RWA_PROBE_MAX_CONCURRENCY=3",
            )
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    for name in (
        "BLOCKSIZE_API_KEY",
        "RWA_MUTATIONS_ENABLED",
        "RWA_OPERATOR_TOKEN",
        "RWA_OBSERVATION_DB_PATH",
        "RWA_STORE_LOCK_TIMEOUT_SECONDS",
        "RWA_PROBE_CALL_TIMEOUT_SECONDS",
        "RWA_PROBE_TOTAL_TIMEOUT_SECONDS",
        "RWA_PROBE_MAX_CONCURRENCY",
    ):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from src.rwa_security import (configured_rwa_observation_db_path, "
                "rwa_mutations_enabled, rwa_operator_token, "
                "rwa_store_lock_timeout_seconds); "
                "from src import rwa_sourcing_runner as runner; "
                "print(json.dumps({'enabled': rwa_mutations_enabled(), "
                "'token': rwa_operator_token(), "
                "'path': configured_rwa_observation_db_path(), "
                "'lock': rwa_store_lock_timeout_seconds(), "
                "'call': runner._bounded_float_env("
                "'RWA_PROBE_CALL_TIMEOUT_SECONDS', 10.0, 0.1, 30.0), "
                "'total': runner._bounded_float_env("
                "'RWA_PROBE_TOTAL_TIMEOUT_SECONDS', 30.0, 0.1, 60.0), "
                "'concurrency': runner._bounded_int_env("
                "'RWA_PROBE_MAX_CONCURRENCY', 2, 1, 8)}))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "enabled": True,
        "token": OPERATOR_TOKEN,
        "path": str(evidence_path),
        "lock": 0.75,
        "call": 7.5,
        "total": 22.5,
        "concurrency": 3,
    }


def test_unauthorized_probe_executes_no_adapter_or_store_calls(rwa_client):
    registry = Mock()
    store = Mock()
    app.state.rwa_adapter_registry = registry
    app.state.rwa_store = store

    response = rwa_client.post(
        "/v1/rwa/sourcing/probe",
        json={"symbols": ["AMZN/USD"], "persist": True},
    )

    assert response.status_code == 401
    registry.get.assert_not_called()
    store.store_observations_batch.assert_not_called()


def test_store_rejects_content_length_and_chunked_bodies_over_limit(rwa_client):
    oversized = b"x" * (128 * 1024 + 1)
    content_length_response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        content=oversized,
    )
    chunks = (b"x" * 65_536 for _ in range(3))
    chunked_response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        content=chunks,
    )

    assert content_length_response.status_code == 413
    assert chunked_response.status_code == 413
    assert (
        chunked_response.json()["detail"]["error_code"]
        == "RWA_REQUEST_TOO_LARGE"
    )


def test_probe_rejects_depth_and_filter_work_above_limits(rwa_client):
    depth_response = rwa_client.post(
        "/v1/rwa/sourcing/probe",
        headers=AUTHORIZATION,
        json={"depth": 201},
    )
    filters_response = rwa_client.post(
        "/v1/rwa/sourcing/probe",
        headers=AUTHORIZATION,
        json={"symbols": [f"ASSET-{index}" for index in range(11)]},
    )

    assert depth_response.status_code == 422
    assert filters_response.status_code == 422


def test_probe_sanitizes_arbitrary_adapter_exceptions(rwa_client):
    class FailingAdapter:
        venue_id = "kraken_xstocks"

        async def fetch_bidask(self, symbol: str):
            raise RuntimeError("https://vendor.invalid/feed?api_key=do-not-reflect")

    registry = RWAAdapterRegistry()
    registry.register(FailingAdapter())
    app.state.rwa_adapter_registry = registry

    response = rwa_client.post(
        "/v1/rwa/sourcing/probe",
        headers=AUTHORIZATION,
        json={"symbols": ["AMZN/USD"], "limit": 1},
    )

    assert response.status_code == 200
    serialized = response.text
    assert "do-not-reflect" not in serialized
    assert response.json()["results"][0]["message"] == "Upstream adapter request failed."


def test_probe_persist_with_no_matching_jobs_is_valid_zero_work(rwa_client):
    response = rwa_client.post(
        "/v1/rwa/sourcing/probe",
        headers=AUTHORIZATION,
        json={"symbols": ["ZZZ_NO_SUCH_ASSET_987654321"], "persist": True},
    )

    assert response.status_code == 200
    assert response.json()["summary"]["jobs_selected"] == 0
    assert response.json()["summary"]["persisted"] == 0


@pytest.mark.asyncio
async def test_probe_persistence_lock_wait_is_bounded(monkeypatch, tmp_path):
    class ReadyAdapter:
        venue_id = "kraken_xstocks"

        async def fetch_bidask(self, symbol: str):
            return {
                "symbol": symbol,
                "venue": self.venue_id,
                "asset_class": "equity",
                "source_type": "native_l1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "bid": 100.0,
                "ask": 100.1,
            }

    store = RWAObservationStore(str(tmp_path / "locked.db"))
    registry = RWAAdapterRegistry()
    registry.register(ReadyAdapter())
    blocker = sqlite3.connect(store.db_path, timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(
        rwa_sourcing_runner,
        "_filter_jobs",
        lambda payload: [
            {
                "job_id": "test-job",
                "status": "ready_to_probe",
                "venue": "kraken_xstocks",
                "symbol": "AMZN/USD",
            }
        ],
    )
    monkeypatch.setenv("RWA_PROBE_TOTAL_TIMEOUT_SECONDS", "0.2")
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match="temporarily unavailable"):
            await rwa_sourcing_runner.probe_sourcing_jobs(
                {
                    "symbols": ["AMZN/USD"],
                    "limit": 1,
                    "include_order_book": False,
                    "persist": True,
                },
                registry=registry,
                store=store,
            )
    finally:
        blocker.rollback()
        blocker.close()

    assert time.monotonic() - started < 0.5
    assert store.summary()["total_observations"] == 0


@pytest.mark.asyncio
async def test_probe_deadline_rolls_back_a_delayed_worker(monkeypatch, tmp_path):
    class ReadyAdapter:
        venue_id = "kraken_xstocks"

        async def fetch_bidask(self, symbol: str):
            return {
                "symbol": symbol,
                "venue": self.venue_id,
                "asset_class": "equity",
                "source_type": "native_l1",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "bid": 100.0,
                "ask": 100.1,
            }

    class DelayedStore(RWAObservationStore):
        def store_observations_batch(self, payloads, **kwargs):
            time.sleep(0.25)
            return super().store_observations_batch(payloads, **kwargs)

    store = DelayedStore(str(tmp_path / "deadline.db"))
    registry = RWAAdapterRegistry()
    registry.register(ReadyAdapter())
    monkeypatch.setattr(
        rwa_sourcing_runner,
        "_filter_jobs",
        lambda payload: [
            {
                "job_id": "test-job",
                "status": "ready_to_probe",
                "venue": "kraken_xstocks",
                "symbol": "AMZN/USD",
            }
        ],
    )
    monkeypatch.setenv("RWA_PROBE_TOTAL_TIMEOUT_SECONDS", "0.1")

    with pytest.raises(TimeoutError):
        await rwa_sourcing_runner.probe_sourcing_jobs(
            {
                "symbols": ["AMZN/USD"],
                "limit": 1,
                "include_order_book": False,
                "persist": True,
            },
            registry=registry,
            store=store,
        )

    assert store.summary()["total_observations"] == 0


@pytest.mark.asyncio
async def test_standalone_store_lock_never_blocks_event_loop(monkeypatch, tmp_path):
    store = RWAObservationStore(str(tmp_path / "off-loop.db"))
    blocker = sqlite3.connect(store.db_path, timeout=1)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setenv("RWA_STORE_LOCK_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    task = asyncio.create_task(
        _store_rwa_observation_without_blocking(store, _observation_payload())
    )
    try:
        await asyncio.sleep(0.05)
        assert time.monotonic() - started < 0.2
    finally:
        blocker.rollback()
        blocker.close()

    result = await task
    assert result["inserted"] is True


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "access_token",
        "privateKey",
        "payment_signature",
        "auth",
        "aws_secret_access_key",
        "aws_access_key_id",
        "mnemonic",
        "seed_phrase",
        "passphrase",
        "proxy_authorization",
        "secret_key",
        "signing_key",
        "encryption_key",
    ],
)
def test_store_rejects_secret_bearing_evidence(rwa_client, field_name):
    payload = _observation_payload()
    payload["metadata"][field_name] = "must-not-be-stored"

    response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        json=payload,
    )

    assert response.status_code == 422
    assert "must-not-be-stored" not in response.text


@pytest.mark.parametrize(
    "secret_value",
    [
        "AWS_SECRET_ACCESS_KEY=must-not-be-stored",
        "-----BEGIN PRIVATE KEY-----\nmust-not-be-stored\n-----END PRIVATE KEY-----",
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signaturevalue",
    ],
)
def test_store_rejects_unmistakable_credential_values(rwa_client, secret_value):
    payload = _observation_payload()
    payload["metadata"]["opaque_value"] = secret_value

    response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        json=payload,
    )

    assert response.status_code == 422
    assert secret_value not in response.text


@pytest.mark.parametrize("query_key", ["api_key", "token"])
def test_store_rejects_credentials_embedded_in_urls(rwa_client, query_key):
    payload = _observation_payload()
    payload["metadata"]["source_url"] = (
        f"https://vendor.invalid/feed?{query_key}=must-not-be-stored"
    )

    response = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        json=payload,
    )

    assert response.status_code == 422
    assert "must-not-be-stored" not in response.text


@pytest.mark.asyncio
async def test_probe_total_timeout_includes_semaphore_queue_wait(monkeypatch):
    monkeypatch.setenv("RWA_PROBE_TOTAL_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(rwa_sourcing_runner, "_PROBE_SEMAPHORE", asyncio.Semaphore(0))
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await rwa_sourcing_runner.probe_sourcing_jobs({"limit": 1})

    assert time.monotonic() - started < 0.5


def test_observation_list_is_operator_only(rwa_client):
    missing = rwa_client.get("/v1/rwa/observations")
    authorized = rwa_client.get(
        "/v1/rwa/observations",
        headers={"X-RWA-Operator-Token": OPERATOR_TOKEN},
    )

    assert missing.status_code == 401
    assert authorized.status_code == 200


def test_public_benchmark_rejects_persistence_before_upstream_call(rwa_client, tmp_path):
    blocksize = AsyncMock()
    app.state.blocksize = blocksize
    app.state.credits = CreditManager(str(tmp_path / "credits.db"))
    store = RWAObservationStore(str(tmp_path / "benchmark-evidence.db"))
    app.state.rwa_store = store

    response = rwa_client.post(
        "/v1/rwa/benchmark/blocksize",
        headers={"X-AGENT-ID": "agent-stateless-benchmark-12345678"},
        json={
            "persist": True,
            "observations": [
                {
                    "symbol": "AAPL/USD",
                    "venue": "kraken_xstocks",
                    "source_type": "native_l2",
                    "value": 100.0,
                }
            ],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "RWA_PUBLIC_PERSISTENCE_FORBIDDEN"
    assert "PAYMENT-REQUIRED" not in response.headers
    assert "PAYMENT-RESPONSE" not in response.headers
    assert store.summary()["total_observations"] == 0
    blocksize.get_bidask_snapshot.assert_not_awaited()


def test_readiness_rejects_rwa_and_observability_database_collision(
    rwa_client,
    monkeypatch,
):
    monkeypatch.setenv("RWA_OBSERVATION_DB_PATH", settings.server.observability_db_path)

    response = rwa_client.get("/readyz")

    assert response.status_code == 503
    check = response.json()["checks"]["rwa_operator_store"]
    assert check["database_isolated"] is False
    assert check["ready"] is False

    mutation = rwa_client.post(
        "/v1/rwa/observations/store",
        headers=AUTHORIZATION,
        json=_observation_payload(),
    )
    assert mutation.status_code == 503
    assert mutation.json()["detail"]["error_code"] == "RWA_DATABASE_NOT_ISOLATED"


def test_readiness_rejects_live_credit_database_collision(rwa_client):
    rwa_path = os.environ["RWA_OBSERVATION_DB_PATH"]
    app.state.credits = CreditManager(rwa_path)

    response = rwa_client.get("/readyz")

    assert response.status_code == 503
    check = response.json()["checks"]["rwa_operator_store"]
    assert "credits_runtime" in check["database_collisions"]
    assert check["ready"] is False


@pytest.mark.parametrize(
    "environment_name",
    [
        "ANTHROPIC_ENTITLEMENT_DB_PATH",
        "CURSOR_ENTITLEMENT_DB_PATH",
        "OPENAI_ENTITLEMENT_DB_PATH",
    ],
)
def test_readiness_rejects_connector_entitlement_database_collision(
    rwa_client,
    monkeypatch,
    environment_name,
):
    monkeypatch.setenv(environment_name, os.environ["RWA_OBSERVATION_DB_PATH"])

    response = rwa_client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["rwa_operator_store"]["database_isolated"] is False
