"""Measure Groq's prompt cache on a scripted owner call: the old layout vs the new one.

    python scripts/groq_cache_check.py            # needs GROQ_API_KEY and the profile data

Each turn sends the owner's real system prompt (persona, compact profile) and the owner's
tool schemas, then appends the model's own reply, like a call. Prints prompt and cached
tokens per turn. "old": the local time inside the first system message and a 12-message
window (the layout before 2026-10-16); "new": app/llm.to_chat_messages.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.getenv("VALR_ENV_FILE") or Path(__file__).resolve().parents[1] / ".env")
from groq import Groq  # noqa: E402

from app import interviews, llm, tools  # noqa: E402

TURNS = ["Hi, what can you do for me today?", "How should I warm up before an interview?",
         "Which of those is the most technical one, do you think?",
         "How should I prepare for a system design round in general?",
         "Give me one likely question for a voice AI founding engineer role.",
         "Thanks, that's all for now."]


def old_layout(history, memory_block, now):
    zone = "Asia/Kolkata"
    hour = now.strftime("%I").lstrip("0")
    system = (f"{llm.SYSTEM_PROMPT} The user's local time is {now:%A, %d %B %Y}, "
              f"{hour}:{now:%M %p} ({zone}).") + f"\n\n{memory_block}"
    turns = [m for m in history]
    current = max(i for i, m in enumerate(turns) if m["role"] == "user")
    window = turns[:current][-12:] + turns[current:]
    return [{"role": "system", "content": system}] + [
        {"role": "assistant" if m["role"] == "model" else "user", "content": m["parts"][0]["text"]}
        for m in window]


def run(layout: str) -> list[tuple[int, int]]:
    client = Groq()
    memory_block = interviews.OWNER_RULES.format(name="Chirag") + "\n\n<profile>\n" + \
        interviews.compact(interviews.profile_block(), interviews.EVALUATION_CHARS_ON_CALLS) + "\n</profile>"
    schemas = tools.OWNER_SCHEMAS
    history, out = [], []
    start = datetime.now(ZoneInfo("Asia/Kolkata"))
    for index, text in enumerate(TURNS):
        now = start + timedelta(minutes=index)  # A minute per turn: the old layout's time changes.
        history.append({"role": "user", "parts": [{"text": text}], "status": "completed"})
        messages = (llm.to_chat_messages(history, memory_block, now) if layout == "new"
                    else old_layout(history, memory_block, now))
        response = client.chat.completions.create(
            model=os.getenv("GROQ_LLM_MODEL", llm.DEFAULT_MODEL), messages=messages, tools=schemas,
            tool_choice="none", reasoning_effort="low", max_completion_tokens=300)
        usage = response.usage
        cached = llm.cached_tokens(usage)
        out.append((usage.prompt_tokens, cached))
        reply = (response.choices[0].message.content or "").strip() or "Okay."
        history.append({"role": "model", "parts": [{"text": reply}], "status": "completed"})
        print(f"{layout} turn {index + 1}: {usage.prompt_tokens} prompt tokens, {cached} cached "
              f"({100 * cached / usage.prompt_tokens:.0f}%), uncached {usage.prompt_tokens - cached}")
        time.sleep(4)
    return out


if __name__ == "__main__":
    results = {layout: run(layout) for layout in sys.argv[1:] or ["old", "new"]}
    for layout, rows in results.items():
        prompt = sum(p for p, _ in rows)
        cached = sum(c for _, c in rows)
        print(json.dumps({"layout": layout, "turns": len(rows), "prompt_tokens": prompt,
                          "cached": cached, "hit_pct": round(100 * cached / prompt, 1),
                          "uncached_per_turn": round((prompt - cached) / len(rows))}))
