"""Gmail OAuth: the signed state, token encryption, the consent URL and token refresh."""
import asyncio
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from app import google_oauth

KEY = Fernet.generate_key().decode()
SETTINGS = google_oauth.OAuthSettings("cid", "csecret", "https://valr.example/oauth/google/callback",
                                      KEY, "owner@example.com")


def test_state_is_signed_and_expires():
    state = google_oauth.make_state(KEY)
    assert google_oauth.check_state(KEY, state)
    assert not google_oauth.check_state(Fernet.generate_key().decode(), state)
    assert not google_oauth.check_state(KEY, state + "x")
    assert not google_oauth.check_state(KEY, google_oauth.make_state(KEY, now=time.time() - 3600))
    assert not google_oauth.check_state(KEY, "garbage")


def test_refresh_tokens_are_stored_encrypted():
    sealed = google_oauth.encrypt(KEY, "1//refresh")
    assert "refresh" not in sealed
    assert google_oauth.decrypt(KEY, sealed) == "1//refresh"
    with pytest.raises(google_oauth.ReconnectNeeded):
        google_oauth.decrypt(Fernet.generate_key().decode(), sealed)


def test_consent_asks_only_for_gmail_readonly_offline():
    url = google_oauth.consent_url(SETTINGS, "s")
    assert "gmail.readonly" in url and "access_type=offline" in url and "prompt=consent" in url
    assert "login_hint=owner%40example.com" in url and "gmail.send" not in url


class FakeConnection:
    def __init__(self, row):
        self.row, self.updates = row, []

    async def execute(self, query, params=()):
        if query.startswith("UPDATE"):
            self.updates.append((query, params))
        connection = self

        class Cursor:
            async def fetchone(self):
                return connection.row
        return Cursor()


def run_refresh(row, status, body):
    def handler(request):
        return httpx.Response(status, json=body)
    connection = FakeConnection(row)

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await google_oauth.access_token(connection, SETTINGS, http)
    return connection, main


def test_refresh_gives_an_access_token():
    connection, main = run_refresh((google_oauth.encrypt(KEY, "r"), "connected"), 200,
                                   {"access_token": "ya29"})
    assert asyncio.run(main()) == "ya29"
    assert "last_ok_at" in connection.updates[0][0]


def test_expired_testing_mode_token_marks_reconnect_needed():
    connection, main = run_refresh((google_oauth.encrypt(KEY, "r"), "connected"), 400,
                                   {"error": "invalid_grant"})
    with pytest.raises(google_oauth.ReconnectNeeded):
        asyncio.run(main())
    assert "reconnect_needed" in connection.updates[0][0]


def test_not_connected_and_already_expired():
    _, main = run_refresh(None, 200, {})
    with pytest.raises(google_oauth.NotConfigured):
        asyncio.run(main())
    _, main = run_refresh((google_oauth.encrypt(KEY, "r"), "reconnect_needed"), 200, {})
    with pytest.raises(google_oauth.ReconnectNeeded):
        asyncio.run(main())
