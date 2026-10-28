"""/static with browser caching that fits how the pages link it.

The pages link every script and stylesheet as /static/<name>?v=<content hash>
(app/valr_ui/shell.asset), so a versioned URL never changes: the browser may keep it for a
year without asking again ("immutable"). Without a header, iOS revalidated each file on every
page after Cloudflare's 4 h default, one round trip per asset before the page could run.
An unversioned URL (icons, fonts, the offline page) keeps a short cache and is revalidated.
"""
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

VERSIONED = "public, max-age=31536000, immutable"
UNVERSIONED = "public, max-age=3600"


def cache_control(query_string: bytes) -> str:
    """The Cache-Control for a static file requested with this query string."""
    params = query_string.decode("latin-1").split("&") if query_string else []
    return VERSIONED if any(p.startswith("v=") and len(p) > 2 for p in params) else UNVERSIONED


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["Cache-Control"] = cache_control(scope.get("query_string", b""))
        return response
