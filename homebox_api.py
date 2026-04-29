"""HomeBox API client. Reads EMAIL, PASSWORD, URL from environment variables."""

import os
import asyncio
import httpx


class HomeBoxError(Exception):
    pass


class HomeBoxClient:
    def __init__(self):
        try:
            self.email = os.environ["EMAIL"]
            self.password = os.environ["PASSWORD"]
            base = os.environ["URL"].rstrip("/")
        except KeyError as e:
            raise HomeBoxError(f"Missing environment variable: {e}") from e

        self.base_url = f"{base}/api/v1"
        self._token: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        await self.login()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def login(self):
        resp = await self._client.post(
            f"{self.base_url}/users/login",
            json={"username": self.email, "password": self.password},
        )
        if resp.status_code != 200:
            raise HomeBoxError(f"Login failed (HTTP {resp.status_code})")
        data = resp.json()
        self._token = data.get("token")
        if not self._token:
            raise HomeBoxError("Login response missing token")

    def _headers(self) -> dict:
        # Token already includes "Bearer " prefix in this API version
        return {"Authorization": self._token}

    async def _get(self, path: str, params: dict | None = None) -> any:
        resp = await self._client.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Items ---

    async def get_items(
        self,
        q: str | None = None,
        page: int = 1,
        page_size: int = 50,
        tags: list[str] | None = None,
        locations: list[str] | None = None,
    ) -> dict:
        params: dict = {"page": page, "pageSize": page_size}
        if q:
            params["q"] = q
        if tags:
            params["tags"] = tags
        if locations:
            params["locations"] = locations
        return await self._get("/items", params=params)

    async def get_all_items(
        self,
        q: str | None = None,
        tags: list[str] | None = None,
        locations: list[str] | None = None,
    ) -> list[dict]:
        """Fetch all pages of items."""
        first = await self.get_items(q=q, page=1, page_size=100, tags=tags, locations=locations)
        total = first.get("total", 0)
        items = first.get("items", [])
        page = 2
        while len(items) < total:
            batch = await self.get_items(q=q, page=page, page_size=100, tags=tags, locations=locations)
            items.extend(batch.get("items", []))
            page += 1
        return items

    async def get_item(self, item_id: str) -> dict:
        return await self._get(f"/items/{item_id}")

    # --- Locations ---

    async def get_locations(self) -> list[dict]:
        return await self._get("/locations")

    async def get_location(self, location_id: str) -> dict:
        return await self._get(f"/locations/{location_id}")

    async def get_location_tree(self) -> list[dict]:
        return await self._get("/locations/tree")

    # --- Tags (called "labels" in older docs, "tags" in v0.24+) ---

    async def get_tags(self) -> list[dict]:
        return await self._get("/tags")

    async def get_tag(self, tag_id: str) -> dict:
        return await self._get(f"/tags/{tag_id}")

    # --- Statistics ---

    async def get_stats(self) -> dict:
        return await self._get("/groups/statistics")

    # --- User ---

    async def get_self(self) -> dict:
        data = await self._get("/users/self")
        # API wraps response in {"item": {...}}
        return data.get("item", data)


def run_client(coro):
    """Run an async coroutine that receives a connected HomeBoxClient."""
    async def _runner():
        async with HomeBoxClient() as client:
            return await coro(client)
    return asyncio.run(_runner())
