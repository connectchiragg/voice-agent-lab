"""Settings › API keys: the workspace's own LLM and Deepgram keys (app/user_keys_web.py).

The owner's workspace runs on the server's keys and gets one line saying so. Anyone else gets
two small forms (LLM, Voice): provider, key (a password field, never shown again), optional
model; Save checks the key first, Test checks it again, Remove asks first. The saved key's
summary ("OpenAI · gpt-6-luna · key ending …1234") and status come from GET /valr/api/keys,
filled in by static/valr-keys.js.
"""
from app import key_providers, tenant
from app.valr_page import chip, e
from app.valr_ui.shell import asset, icon

HELP = ("VALR runs on your own keys, so you pay the provider directly. ChatGPT, Claude or Gemini "
        "subscriptions can't be used here: you need an API key from the provider's console. Deepgram "
        "gives new accounts free credit.")
PURPOSES = (("llm", "Language model", "Briefings, research, coaching and the replies on calls."),
            ("speech", "Voice", "Deepgram: hearing you and speaking on browser calls."))


def confirm(title: str, body: str, ok: str) -> str:
    return f' data-confirm="{e(title)}" data-confirm-body="{e(body)}" data-confirm-ok="{e(ok)}"'


def form(purpose: str, title: str, use: str) -> str:
    options = "".join(f'<option value="{e(p.name)}" data-default-model="{e(p.default_model)}" '
                      f'data-console="{e(p.console)}">{e(p.label)}</option>'
                      for p in key_providers.for_purpose(purpose))
    model = "" if purpose != "llm" else (
        f'<label class="field-label" for="keys-{purpose}-model">Model <span class="muted">(optional)</span></label>'
        f'<input class="input" id="keys-{purpose}-model" name="model" maxlength="{key_providers.MODEL_CHARS}" '
        'autocomplete="off" spellcheck="false" data-keys-model>')
    return f"""
<form class="inline-form keys-form" data-keys-form data-purpose="{purpose}" autocomplete="off" novalidate>
  <h4 class="sub-title">{e(title)}</h4>
  <p class="muted">{e(use)}</p>
  <p data-keys-status>Checking…</p>
  <label class="field-label" for="keys-{purpose}-provider">Provider</label>
  <select class="input" id="keys-{purpose}-provider" name="provider" data-keys-provider>{options}</select>
  <label class="field-label" for="keys-{purpose}-key">API key</label>
  <input class="input" id="keys-{purpose}-key" name="key" type="password" autocomplete="off" spellcheck="false"
    maxlength="400" required aria-describedby="keys-{purpose}-help keys-{purpose}-error">
  <p class="field-help" id="keys-{purpose}-help">Make one at <a data-keys-console target="_blank" rel="noopener noreferrer"></a>.</p>
  {model}
  <p class="field-help bad" id="keys-{purpose}-error" data-keys-error role="alert" hidden></p>
  <div class="rec-buttons"><button class="btn sm primary" type="submit">Save</button>
    <button class="btn sm" type="button" data-keys-test hidden>Test</button>
    <button class="btn sm ghost" type="button" data-keys-remove hidden{confirm(
        "Remove this key?", "Features that use it stop until you add a key again.", "Remove")}>Remove</button></div>
</form>"""


def row() -> str:
    """The API keys row (Settings), shaped like the other settings rows."""
    lead = f'<span class="set-icon" aria-hidden="true">{icon("plug", 18)}</span>'
    if tenant.is_owner():
        body = '<p>Uses the server\'s keys.</p>'
        return (f'<li class="set-row" id="api-keys">{lead}<div class="set-main"><div class="set-title-row">'
                f'<h3 class="set-title">Server keys</h3>{chip("Server", "ok")}</div><div class="set-body">{body}</div></div></li>')
    forms = "".join(form(*purpose) for purpose in PURPOSES)
    return (f'<li class="set-row" id="api-keys">{lead}<div class="set-main"><div class="set-title-row">'
            f'<h3 class="set-title">Your keys</h3></div><div class="set-body"><p>{e(HELP)}</p>{forms}'
            f'<script type="module" src="{asset("valr-keys.js")}"></script></div></div></li>')
