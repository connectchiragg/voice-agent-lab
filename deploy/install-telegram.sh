#!/usr/bin/env bash
# Installs VALR's Telegram bot: run on the Mac AFTER the valr-telegram build is deployed.
#
# 1. Prompts for the bot token from @BotFather (hidden input, never echoed or put on a
#    command line) and generates TELEGRAM_WEBHOOK_SECRET.
# 2. Checks the token with getMe (prints only the bot's @username).
# 3. Writes both to the VM's ~/voice-agent-lab/.env (replacing earlier values, chmod 600)
#    and restarts the app and the research worker so they read them.
# 4. setWebhook https://valr.haciensus.com/hooks/telegram with the secret_token, only
#    message and callback_query updates, pending updates dropped; sets the command menu.
#
# Needs: the Cloudflare Access bypass for valr.haciensus.com/hooks/telegram.
# Then: dashboard > Get set up > Connect Telegram > "Show /start code", and send
# "/start <code>" to the bot within 15 minutes.
set -euo pipefail
VM=ubuntu@<vm-ip>
KEY="$HOME/.ssh/voice-agent-lab_ed25519"
HOOK=https://valr.haciensus.com/hooks/telegram

read -rs -p 'Paste the bot token from @BotFather (hidden): ' token
printf '\n'
case "$token" in
  *[!0-9A-Za-z:_-]*|"") echo "That does not look like a bot token." >&2; exit 1 ;;
esac
case "$token" in
  [0-9]*:*) ;;
  *) echo "That does not look like a bot token." >&2; exit 1 ;;
esac
secret=$(openssl rand -hex 32)

# Token and secret go to Python on stdin: never in argv, the environment or a file.
api() {  # $1: check | webhook
  printf '%s\n%s\n%s\n' "$token" "$secret" "$HOOK" | python3 -I -c '
import json, sys, urllib.request, urllib.error
token, secret, hook = (sys.stdin.readline().strip() for _ in range(3))
def call(method, payload=None):
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                     data=json.dumps(payload or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        body = json.load(error)
    except Exception as error:
        sys.exit(f"{method}: {type(error).__name__}")
    if not body.get("ok"):
        sys.exit(method + " refused: " + str(body.get("description", "")))
    return body["result"]
if sys.argv[1] == "check":
    print("Bot: @" + call("getMe")["username"])
else:
    call("setWebhook", {"url": hook, "secret_token": secret, "max_connections": 5,
                        "allowed_updates": ["message", "callback_query"], "drop_pending_updates": True})
    call("setMyCommands", {"commands": [
        {"command": "next", "description": "My next interview"},
        {"command": "today", "description": "Today’s interviews"},
        {"command": "pause", "description": "Pause the heads-ups"},
        {"command": "resume", "description": "Resume the heads-ups"}]})
    info = call("getWebhookInfo")
    print("Webhook:", info.get("url"), "| pending:", info.get("pending_update_count"),
          "| last error:", info.get("last_error_message") or "none")
' "$1"
}

api check

printf 'TELEGRAM_BOT_TOKEN=%s\nTELEGRAM_WEBHOOK_SECRET=%s\n' "$token" "$secret" \
  | ssh -i "$KEY" -o BatchMode=yes "$VM" '
cd voice-agent-lab && t=$(mktemp) && grep -vE "^TELEGRAM_(BOT_TOKEN|WEBHOOK_SECRET)=" .env > "$t" && cat >> "$t" && cat "$t" > .env && rm -f "$t" && chmod 600 .env
echo "VM .env: $(grep -cE "^TELEGRAM_(BOT_TOKEN|WEBHOOK_SECRET)=" .env) Telegram settings"
sudo -n docker rm -f voice-agent-lab >/dev/null && sudo -n docker run -d --name voice-agent-lab --restart unless-stopped --env-file .env --network voice-agent-lab $(sh deploy/valr-data-live.sh app) -p 127.0.0.1:7860:7860 voice-agent-lab:latest >/dev/null
DOCKER="sudo -n docker" sh deploy/research-setup.sh >/dev/null 2>&1
sleep 8; echo "app health: $(curl -s -o /dev/null -w "%{http_code}" localhost:7860/healthz)"
echo "webhook without the secret (expect 403): $(curl -s -o /dev/null -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "{}" localhost:7860/hooks/telegram)"'

api webhook
unset token secret
echo "Done. Now open https://valr.haciensus.com/valr > Get set up > Connect Telegram > Show /start code,"
echo "and send that /start <code> to the bot within 15 minutes."
