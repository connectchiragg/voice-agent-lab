"""Page speed: static caching, the per-render leads memo and the cached company rules."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import company_names, leads
from app.static_files import VERSIONED, UNVERSIONED, CachedStaticFiles, cache_control
from app.valr_ui import routes


def test_cache_control_by_version_param():
    assert cache_control(b"v=3b4517af43") == VERSIONED
    assert cache_control(b"x=1&v=abc") == VERSIONED
    assert cache_control(b"") == UNVERSIONED
    assert cache_control(b"v=") == UNVERSIONED
    assert cache_control(b"version=1") == UNVERSIONED


def test_static_files_send_the_header(tmp_path):
    (tmp_path / "a.js").write_text("export {};")
    app = FastAPI()
    app.mount("/static", CachedStaticFiles(directory=tmp_path), name="static")
    client = TestClient(app)
    assert client.get("/static/a.js?v=0123456789").headers["cache-control"] == VERSIONED
    assert client.get("/static/a.js").headers["cache-control"] == UNVERSIONED
    assert client.get("/static/missing.js?v=1").status_code == 404


def test_leads_loaded_once_per_render(monkeypatch):
    calls = []

    async def load():
        calls.append(1)
        return {"available": True, "raw": None}

    monkeypatch.setattr(routes, "load_leads", load)

    async def render():
        routes._leads_memo.set({})
        first = await routes.leads_once()
        assert await routes.leads_once() is first

    asyncio.run(render())
    assert len(calls) == 1
    asyncio.run(routes.leads_once())  # Outside a render (the JSON API): no memo, a fresh load.
    assert len(calls) == 2


def scan(domain, names):  # The old linear rule app/company_names.on replaced.
    return any(domain == n or domain.endswith("." + n) for n in names)


def test_on_matches_the_linear_scan():
    names = frozenset({"greenhouse.io", "lever.co", "mail.example.com", "io"})
    for domain in ("greenhouse.io", "boards.greenhouse.io", "notgreenhouse.io", "x.lever.co", "lever.co.uk",
                   "mail.example.com", "a.mail.example.com", "example.com", "anything.io", "", "io", "a..io"):
        assert company_names.on(domain, names) == scan(domain, names), domain
    for domain in ("hirist.tech", "jobs.hirist.tech", "acme.com", "talent.naukri.com"):
        assert company_names.on(domain, company_names.AGENCY_DOMAINS) == scan(domain, company_names.AGENCY_DOMAINS)


def test_agency_brands_cached_and_learned():
    assert company_names.agency_brands() is company_names.agency_brands()
    assert not company_names.is_agency_name("Zyxwq")
    assert company_names.is_agency_name("Zyxwq", ["zyxwq.com"])  # A learned agency domain still counts.
    assert company_names.is_agency_name("Zyxwq Mail", {"zyxwq.com"})


def test_domain_helpers_cached():
    assert leads.domain_company("careers.acme-robotics.com") == "Acme Robotics"
    assert leads.company_domain("careers.acme-robotics.com") == "acme-robotics.com"
    assert leads.company_domain("gmail.com") == ""
    assert leads.domain_company.cache_info().hits + leads.domain_company.cache_info().misses > 0
