"""HomeBox API client. Reads EMAIL, PASSWORD, URL from environment variables."""

import os
import asyncio
import pathlib
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

    async def _post(self, path: str, payload: dict) -> any:
        resp = await self._client.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, payload: dict) -> any:
        resp = await self._client.put(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> None:
        resp = await self._client.delete(
            f"{self.base_url}{path}",
            headers=self._headers(),
        )
        resp.raise_for_status()

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

    async def create_item(self, payload: dict) -> dict:
        """payload: name, description, quantity, locationId"""
        return await self._post("/items", payload)

    async def update_item(self, item_id: str, payload: dict) -> dict:
        return await self._put(f"/items/{item_id}", payload)

    async def delete_item(self, item_id: str) -> None:
        await self._delete(f"/items/{item_id}")

    async def upload_item_image(self, item_id: str, file_path: str) -> dict:
        p = pathlib.Path(file_path).expanduser().resolve()
        suffix = p.suffix.lower().lstrip(".")
        mime = {
            "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "webp": "image/webp",
        }.get(suffix, "application/octet-stream")
        with open(p, "rb") as f:
            content = f.read()
        resp = await self._client.post(
            f"{self.base_url}/items/{item_id}/attachments",
            headers=self._headers(),
            files={"file": (p.name, content, mime)},
            data={"type": "photo", "name": p.name},
        )
        resp.raise_for_status()
        return resp.json()

    # --- Locations ---

    async def get_locations(self) -> list[dict]:
        return await self._get("/locations")

    async def get_location(self, location_id: str) -> dict:
        return await self._get(f"/locations/{location_id}")

    async def get_location_tree(self) -> list[dict]:
        return await self._get("/locations/tree")

    async def create_location(self, name: str, description: str = "", parent_id: str | None = None) -> dict:
        payload: dict = {"name": name, "description": description}
        if parent_id:
            payload["parentId"] = parent_id
        return await self._post("/locations", payload)

    async def update_location(self, location_id: str, name: str, description: str = "", parent_id: str | None = None) -> dict:
        # WARNING: HomeBox PUT clears parentId when it is omitted. Always pass the
        # existing parent_id when you only want to rename — or use rename_location().
        payload: dict = {"name": name, "description": description}
        if parent_id:
            payload["parentId"] = parent_id
        return await self._put(f"/locations/{location_id}", payload)

    async def rename_location(self, location_id: str, new_name: str, description: str | None = None) -> dict:
        """Rename a location without changing its parent or description."""
        existing = await self.get_location(location_id)
        parent_id = None
        if existing.get("parent"):
            parent_id = existing["parent"].get("id")
        desc = description if description is not None else existing.get("description", "")
        return await self.update_location(location_id, new_name, desc, parent_id=parent_id)

    async def delete_location(self, location_id: str) -> None:
        await self._delete(f"/locations/{location_id}")

    # --- Tags (called "labels" in older docs, "tags" in v0.24+) ---

    async def get_tags(self) -> list[dict]:
        return await self._get("/tags")

    async def get_tag(self, tag_id: str) -> dict:
        return await self._get(f"/tags/{tag_id}")

    async def create_tag(self, name: str) -> dict:
        return await self._post("/tags", {"name": name})

    # --- Statistics ---

    async def get_stats(self) -> dict:
        return await self._get("/groups/statistics")

    # --- User ---

    async def get_self(self) -> dict:
        data = await self._get("/users/self")
        # API wraps response in {"item": {...}}
        return data.get("item", data)

    async def get_qrcode(self, data: str) -> bytes:
        """Return raw QR code image bytes for the given data string (e.g. 'loc:<id>')."""
        resp = await self._client.get(
            f"{self.base_url}/qrcode",
            params={"data": data},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.content

    async def get_attachment(self, item_id: str, attachment_id: str) -> bytes:
        resp = await self._client.get(
            f"{self.base_url}/items/{item_id}/attachments/{attachment_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.content


def run_client(coro):
    """Run an async coroutine that receives a connected HomeBoxClient."""
    async def _runner():
        async with HomeBoxClient() as client:
            return await coro(client)
    return asyncio.run(_runner())
