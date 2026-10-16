"""VALR's own sign-in: "Sign in with Google" for its one owner, instead of Cloudflare Access.

How it works
- /login shows the VALR sign-in page; its button goes to /login/google, which saves a
  one-time state and an OpenID nonce (Postgres, 10 minutes) and redirects to Google with
  `openid email profile` and `prompt=select_account` only.
- Google returns to the existing /oauth/google/callback (the Gmail/Calendar client and
  redirect URI are reused; nothing new to register). A state starting "login." is a sign-in:
  the state is deleted as it is read (so it works once), the code is exchanged at Google's
  token endpoint, and the ID token's iss, aud, exp, nonce, email_verified and email are
  checked. The Gmail/Calendar grant is never touched.
- Only an address in VALR_OWNER_EMAILS gets a session. Anyone else sees /private ("VALR is
  private"), gets no session, and the log records the email's domain only.
- The session cookie `__Host-valr` holds 32 random bytes (HttpOnly, Secure, SameSite=Lax,
  30 days); Postgres keeps only their SHA-256 (valr_sessions). POST /logout deletes the row.
- `Gate` (app/auth/gate.py) guards every route but an explicit public list (webhooks that
  check their own secret, /healthz, the sign-in pages, static files): pages redirect to
  /login?next=..., APIs get 401 JSON, WebSockets are refused, and every POST must be
  same-origin. The dashboard's CSRF token on actions stays as it was.

Settings
- VALR_OWNER_EMAILS  comma-separated owner addresses (env only; required for `app`/`both`).
- VALR_AUTH_MODE     access | app | both (default both: Access or a session).
- No new secret: session tokens are random and stored hashed, and states are one-time rows.
  Google's client is GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI (+ VALR_TOKEN_KEY, which
  google_oauth.OAuthSettings already requires).

Cut-over (docs/deploy.md, "Sign-in", has the steps): deploy with the default `both`, sign in once at
/login, remove the Cloudflare Access application in front of the VALR host, then set
VALR_AUTH_MODE=app.
"""
from app.auth.core import Identity, is_login_state, mode, owner_emails, signed_in_email
from app.auth.gate import Gate, identify
from app.auth.routes import finish_login, router

__all__ = ["Gate", "Identity", "finish_login", "identify", "is_login_state", "mode",
           "owner_emails", "router", "signed_in_email"]
