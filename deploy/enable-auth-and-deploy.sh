#!/usr/bin/env bash
# Turns on VALR's own "Sign in with Google" (app/auth) and deploys, run by the owner from the
# Mac at the repo root:
#
#   bash deploy/enable-auth-and-deploy.sh
#   BRANCH=<branch> bash deploy/enable-auth-and-deploy.sh   # default valr-auth, passed to deploy-all.sh
#
# 1. If VALR_OWNER_EMAILS is missing from the VM's ~/voice-agent-lab/.env, asks for it once
#    (the Google address(es) allowed to sign in, comma-separated). The value travels on stdin,
#    never on a command line, and is never printed. Checks the Google OAuth client is there.
# 2. Leaves VALR_AUTH_MODE unset, so the mode is `both`: Cloudflare Access still works, and
#    a VALR session works too. Nobody is locked out by this deploy.
# 3. Runs deploy/deploy-all.sh with BRANCH=valr-auth (it stops at its first failure).
# 4. Prints checks through the app's local port: /healthz, GET /login is 200, and GET /valr
#    without a login is refused (a redirect to /login, or 403).
# 5. Prints the remaining cut-over steps.
# Never touches cloudflared, Cloudflare Access, Temporal's or Postgres' containers, or GCP.
set -euo pipefail
VM=${VM:-ubuntu@<vm-ip>}
KEY=${KEY:-$HOME/.ssh/voice-agent-lab_ed25519}
BRANCH=${BRANCH:-valr-auth}
REPO=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
ssh_vm() { ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$VM" "$@"; }
say() { printf '\n== %s\n' "$*"; }
export VM KEY BRANCH

say "1. Sign-in settings in the VM .env"
ssh_vm 'cd ~/voice-agent-lab && [ -s .env ]' || { echo "No ~/voice-agent-lab/.env on the VM" >&2; exit 1; }
if ssh_vm 'cd ~/voice-agent-lab && grep -qE "^VALR_OWNER_EMAILS=.+" .env'; then
  echo "VALR_OWNER_EMAILS: already set"
else
  value=""
  while [ -z "$value" ]; do
    read -r -s -p "The Google address that may sign in to VALR (VALR_OWNER_EMAILS; comma-separate several): " value < /dev/tty
    echo
    case $value in *@*) ;; *) echo "That doesn't look like an email address; try again."; value="" ;; esac
  done
  # The value travels on stdin, never on a command line or in the output.
  printf '%s\n' "$value" | ssh_vm "bash -c 'cd ~/voice-agent-lab && IFS= read -r v && t=\$(mktemp) && { grep -vE \"^VALR_OWNER_EMAILS=\" .env > \"\$t\" || true; } && printf \"VALR_OWNER_EMAILS=%s\\n\" \"\$v\" >> \"\$t\" && cat \"\$t\" > .env && rm -f \"\$t\" && chmod 600 .env && echo \"VALR_OWNER_EMAILS: set\"'"
  unset value
fi
ssh_vm 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/voice-agent-lab
if grep -qE "^VALR_AUTH_MODE=" .env; then
  echo "NOTE: VALR_AUTH_MODE is set in .env ($(grep -E '^VALR_AUTH_MODE=' .env | tail -1 | cut -d= -f2-)); the cut-over expects it unset (both) for now."
else
  echo "VALR_AUTH_MODE: unset (both: Cloudflare Access or a VALR session)"
fi
for k in GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET VALR_TOKEN_KEY; do
  if grep -qE "^$k=.+" .env; then echo "$k: present"; else echo "MISSING: $k (sign-in needs the Google OAuth client)"; fi
done
REMOTE

say "2. Deploy $BRANCH (deploy/deploy-all.sh)"
BRANCH=$BRANCH bash "$REPO/deploy/deploy-all.sh"

say "3. Checks (inside the app container, port 7860)"
ssh_vm 'bash -s' <<'REMOTE'
set -uo pipefail
sleep 10
sudo docker exec -i voice-agent-lab python - <<'PY'
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


open_ = urllib.request.build_opener(NoRedirect).open
HOST = {"Host": "valr.haciensus.com"}


def get(path):
    try:
        response = open_(urllib.request.Request("http://127.0.0.1:7860" + path, headers=HOST), timeout=5)
        return response.status, response.headers.get("location", ""), response.read(300).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("location", ""), ""
    except Exception as error:  # noqa: BLE001
        return None, "", type(error).__name__


status, _, body = get("/healthz")
print(f"healthz: {status} {body.strip()}")
status, _, body = get("/login")
print(f"GET /login: {status} " + ("OK" if status == 200 else "UNEXPECTED: it should be 200"))
status, location, _ = get("/valr")
ok = (status in (302, 303, 307) and location.startswith("/login")) or status == 403
print(f"GET /valr without a login: {status} {location} " + ("OK (refused)" if ok else "UNEXPECTED: it should be refused"))
PY
REMOTE

say "Next steps (the cut-over)"
cat <<'TEXT'
VALR now accepts both your Cloudflare Access login and its own Google sign-in.
1. Open https://valr.haciensus.com/login, press "Sign in with Google" and pick your account.
   You should land on the VALR Home page, with your initial and "Sign out" at the top right.
2. In Cloudflare Zero Trust, delete the Access application "voice" for valr.haciensus.com.
   Reload https://valr.haciensus.com/valr: you should still be in (the VALR sign-in alone).
3. Then make VALR's sign-in the only way in: add VALR_AUTH_MODE=app to ~/voice-agent-lab/.env
   on the VM and run this deploy again (or restart the app container).
To go back: set VALR_AUTH_MODE=access and restore the Access application.
TEXT
