"""The VALR web app's pages: a small multi-page product over app/valr_view.collect().

- shell.py: the page frame (navigation, the needs-attention badge, theme, footer).
- attention.py: one "needs attention" model (what is broken, with a one-click fix).
- contracts.py: the data the leads, notetaker and coach branches plug in, normalised.
- home.py, interviews.py, leads.py, performance.py, settings.py, system.py: the pages.
- routes.py: GET routes (Cloudflare Access on every one; actions stay in valr_dashboard).
"""
