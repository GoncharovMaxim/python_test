import httpx
import pytest

import helpdesk_mock.main as mock
from tests.conftest import assert_logged


@pytest.fixture
async def mock_client(monkeypatch):
    state = mock.State(
        config=mock.Config(reply_fail_rate=0, reply_lost_response_rate=0)
    )
    monkeypatch.setattr(mock, "state", state)
    transport = httpx.ASGITransport(app=mock.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://mock"
    ) as client:
        yield client, state


async def test_reply_with_same_idempotency_key_is_stored_once(mock_client):
    client, state = mock_client
    body = {"ticket_id": "T-1", "message": "Відповідь"}
    headers = {"Idempotency-Key": "key-1"}
    first = await client.post(
        "/tickets/T-1/replies", json=body, headers=headers
    )
    second = await client.post(
        "/tickets/T-1/replies", json=body, headers=headers
    )
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert len(state.replies) == 1
    assert state.stats["duplicate_messages"] == 0


async def test_mock_detects_duplicates_without_key(mock_client, caplog):
    client, state = mock_client
    body = {"ticket_id": "T-1", "message": "Відповідь"}
    await client.post("/tickets/T-1/replies", json=body)
    await client.post("/tickets/T-1/replies", json=body)
    assert state.stats["duplicate_messages"] == 1
    assert_logged(caplog, "ERROR", "DUPLICATE reply to ticket T-1")


async def test_mock_failure_does_not_store(mock_client):
    client, state = mock_client
    state.config.reply_fail_rate = 1.0
    response = await client.post(
        "/tickets/T-1/replies", json={"ticket_id": "T-1", "message": "x"}
    )
    assert response.status_code >= 500
    assert state.replies == []
