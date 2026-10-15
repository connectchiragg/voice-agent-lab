"""ICE servers for WebRTC calls: short-lived Cloudflare Realtime TURN credentials.

Each call mints its own credentials, valid only for the call time limit, from
a TURN key (CF_TURN_KEY_ID + CF_TURN_KEY_API_TOKEN). Without a key, calls fall
back to Cloudflare's public STUN server, which only works when the server is
directly reachable over UDP (for example on localhost).
"""
import logging
import os
from contextlib import nullcontext
from typing import Any

import httpx

logger = logging.getLogger("uvicorn.error")
CLOUDFLARE_STUN = {"urls": ["stun:stun.cloudflare.com:3478"]}
GENERATE_URL = "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers"

IceServer = dict[str, Any]


def turn_configured() -> bool:
    return bool(os.getenv("CF_TURN_KEY_ID") and os.getenv("CF_TURN_KEY_API_TOKEN"))


def browser_usable(servers: list[IceServer]) -> list[IceServer]:
    """Drop port-53 URLs: browsers block them, so they only slow ICE gathering down."""
    usable = []
    for server in servers:
        urls = server["urls"] if isinstance(server["urls"], list) else [server["urls"]]
        urls = [url for url in urls if ":53?" not in url and not url.endswith(":53")]
        if urls:
            usable.append({**server, "urls": urls})
    return usable


async def mint_ice_servers(ttl_s: int, client: httpx.AsyncClient | None = None) -> list[IceServer]:
    """Fresh STUN + TURN servers with credentials expiring after `ttl_s`.

    Raises httpx.HTTPError if Cloudflare refuses or is unreachable; never logs
    the credentials.
    """
    if not turn_configured():
        return [CLOUDFLARE_STUN]
    url = GENERATE_URL.format(key_id=os.environ["CF_TURN_KEY_ID"])
    headers = {"Authorization": f"Bearer {os.environ['CF_TURN_KEY_API_TOKEN']}"}
    async with httpx.AsyncClient(timeout=5) if client is None else nullcontext(client) as http:
        response = await http.post(url, headers=headers, json={"ttl": ttl_s})
    response.raise_for_status()
    servers = response.json()["iceServers"]
    if isinstance(servers, dict):  # The older single-server response shape.
        servers = [servers]
    return browser_usable(servers)

