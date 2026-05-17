import httpx
from typing import Optional

API_BASE = "https://api.cyphergoat.com"


class CypherGoatError(Exception):
    pass


class CypherGoatClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        await self._client.aclose()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def estimate(
        self,
        coin1: str,
        network1: str,
        coin2: str,
        network2: str,
        amount: float,
    ) -> dict:
        resp = await self._client.get(
            f"{API_BASE}/estimate",
            params={
                "coin1": coin1,
                "network1": network1,
                "coin2": coin2,
                "network2": network2,
                "amount": amount,
            },
            headers=self._auth_headers(),
        )
        if resp.status_code != 200:
            data = resp.json()
            raise CypherGoatError(data.get("error", resp.text))
        return resp.json()

    async def swap(
        self,
        coin1: str,
        network1: str,
        coin2: str,
        network2: str,
        amount: float,
        partner: str,
        address: str,
    ) -> dict:
        resp = await self._client.get(
            f"{API_BASE}/swap",
            params={
                "coin1": coin1,
                "network1": network1,
                "coin2": coin2,
                "network2": network2,
                "amount": amount,
                "partner": partner,
                "address": address,
            },
            headers=self._auth_headers(),
        )
        if resp.status_code != 200:
            data = resp.json()
            raise CypherGoatError(data.get("error", resp.text))
        return resp.json()

    async def transaction(self, cgid: str) -> dict:
        resp = await self._client.get(
            f"{API_BASE}/transaction",
            params={"id": cgid},
        )
        if resp.status_code == 404:
            raise CypherGoatError(f"Transaction {cgid} not found")
        if resp.status_code != 200:
            data = resp.json()
            raise CypherGoatError(data.get("error", resp.text))
        return resp.json()
