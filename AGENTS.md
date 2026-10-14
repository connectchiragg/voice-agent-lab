# Working agreement

- The owner designs the architecture and makes the product and design decisions.
  Coding agents implement, test and explain. Present trade-offs with a
  recommendation; do not silently choose between materially different designs.
- Do not add frameworks, paid services or infrastructure without agreement.
  The stack is open-weight and free: local models plus Groq's free tier.
- Measure before claiming: benchmark model choices on real data, and report
  latency and accuracy numbers instead of impressions.
- Test each increment (`python -m pytest`, `node --test tests/audio-player.test.mjs`)
  and state what is intentionally incomplete or unverified.
- Keep the realtime audio path (`app/pipeline.py`) free of slow or blocking work;
  durable or long-running work belongs outside it.
- Prefer explicit control flow, small functions and minimal dependencies. Keep
  transport (`app/main.py`) separate from conversation logic.
- Never commit API keys, recordings, model weights or private conversation data.
