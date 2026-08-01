"""Tests for HomeBoxClient.update_location.

Unit tests mock HTTP; the live integration test hits the real instance and
cleans up after itself.  Run live tests with:
    set -a && source .env && set +a
    HOMEBOX_URL=... HOMEBOX_EMAIL=... HOMEBOX_PASSWORD=... pytest test_update_location.py -v
"""

import asyncio
import json
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Helpers to build a mocked HomeBoxClient without real env vars
# ---------------------------------------------------------------------------

def _make_client(base_url="http://fake/api/v1", token="tok"):
    from homebox_api import HomeBoxClient
    with patch.dict(os.environ, {"EMAIL": "a@b.com", "PASSWORD": "pw", "URL": "http://fake"}):
        client = HomeBoxClient.__new__(HomeBoxClient)
    client.base_url = base_url
    client._token = token
    client._client = None
    return client


def _mock_response(data: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestUpdateLocationUnit:
    def test_sends_put_to_correct_path(self):
        client = _make_client()
        returned = {"id": "abc", "name": "New Name", "description": ""}
        mock_resp = _mock_response(returned)
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        result = asyncio.run(client.update_location("abc", "New Name"))

        mock_http.put.assert_called_once_with(
            "http://fake/api/v1/locations/abc",
            headers=client._headers(),
            json={"name": "New Name", "description": ""},
        )
        assert result == returned

    def test_includes_description_when_provided(self):
        client = _make_client()
        mock_resp = _mock_response({"id": "abc", "name": "X", "description": "desc"})
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        asyncio.run(client.update_location("abc", "X", description="desc"))

        _, kwargs = mock_http.put.call_args
        assert kwargs["json"]["description"] == "desc"

    def test_includes_parent_id_when_provided(self):
        client = _make_client()
        mock_resp = _mock_response({"id": "abc", "name": "X", "description": ""})
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        asyncio.run(client.update_location("abc", "X", parent_id="parent-uuid"))

        _, kwargs = mock_http.put.call_args
        assert kwargs["json"]["parentId"] == "parent-uuid"

    def test_omits_parent_id_when_none(self):
        client = _make_client()
        mock_resp = _mock_response({"id": "abc", "name": "X", "description": ""})
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        asyncio.run(client.update_location("abc", "X"))

        _, kwargs = mock_http.put.call_args
        assert "parentId" not in kwargs["json"]

    def test_raises_on_http_error(self):
        client = _make_client()
        mock_resp = _mock_response({}, status=404)
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        with pytest.raises(Exception, match="404"):
            asyncio.run(client.update_location("nonexistent", "X"))

    def test_returns_api_response(self):
        client = _make_client()
        api_data = {"id": "abc", "name": "Renamed", "description": "d", "itemCount": 3}
        mock_resp = _mock_response(api_data)
        mock_http = AsyncMock()
        mock_http.put = AsyncMock(return_value=mock_resp)
        client._client = mock_http

        result = asyncio.run(client.update_location("abc", "Renamed", description="d"))
        assert result["name"] == "Renamed"
        assert result["id"] == "abc"


# ---------------------------------------------------------------------------
# Live integration test (skipped when env vars are absent)
# ---------------------------------------------------------------------------

LIVE = (
    os.environ.get("HOMEBOX_URL") and
    os.environ.get("HOMEBOX_EMAIL") and
    os.environ.get("HOMEBOX_PASSWORD")
)

@pytest.mark.skipif(not LIVE, reason="HOMEBOX_URL/EMAIL/PASSWORD not set")
class TestUpdateLocationLive:
    """Creates a temporary location, renames it, then deletes it."""

    def test_create_rename_delete(self):
        from homebox_api import HomeBoxClient, run_client

        os.environ["URL"] = os.environ["HOMEBOX_URL"]
        os.environ["EMAIL"] = os.environ["HOMEBOX_EMAIL"]
        os.environ["PASSWORD"] = os.environ["HOMEBOX_PASSWORD"]

        async def _run(client):
            # Create
            created = await client.create_location("__test_location_rename__")
            loc_id = created["id"]
            assert created["name"] == "__test_location_rename__"

            # Rename
            updated = await client.update_location(loc_id, "__test_location_renamed__", description="test desc")
            assert updated["name"] == "__test_location_renamed__"
            assert updated["description"] == "test desc"

            # Verify via GET
            fetched = await client.get_location(loc_id)
            assert fetched["name"] == "__test_location_renamed__"

            # Cleanup
            await client.delete_location(loc_id)

            return updated

        result = run_client(_run)
        assert result["name"] == "__test_location_renamed__"
