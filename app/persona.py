"""The assistant's name: displayed as ASSISTANT_NAME ("VALR"), spoken as SPOKEN_NAME.

Aura-2 (odysseus) was checked on 2026-10-16 by synthesizing "Hi Chirag, it's <name>"
and transcribing it back with Deepgram nova-3: "Valor" came back as "Valor" every
time (0.48 s, the shortest and clearest); "Valer" was heard as "Fowler"; "Valar" and
"Vahl-er" as "Valar". So prompts and captions say VALR, and `app.deepgram.speakable`
swaps in SPOKEN_NAME just before text goes to Aura.
"""
import os
import re

# Words respelled for Aura only (captions and history keep the real spelling). Checked
# on 2026-10-16 by synthesizing each spelling three times and transcribing it back with
# nova-3, plus a listening sample set: "Bright Line" puts the word boundary where a
# listener expects it (nova-3 hears "Bright Lion" for either spelling: an ASR bias,
# not the voice). "Northwind" and "Chirag" are best as written ("North-wind" was heard as
# "North Window"); Aura-2's inline IPA control was read aloud as JSON, so it is unused.
PRONOUNCE = {"Brightline": "Bright Line", "BrightLine": "Bright Line"}
PRONOUNCE_PATTERN = re.compile(r"\b(" + "|".join(map(re.escape, PRONOUNCE)) + r")\b")

ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "VALR")
SPOKEN_NAME = os.getenv("ASSISTANT_SPOKEN_NAME", "Valor")
NAME_PATTERN = re.compile(rf"\b{re.escape(ASSISTANT_NAME)}\b")


def spoken(text: str) -> str:
    """Text for the TTS: the displayed name replaced by its phonetic spelling, and the
    respellings in PRONOUNCE."""
    text = NAME_PATTERN.sub(SPOKEN_NAME, text) if ASSISTANT_NAME != SPOKEN_NAME else text
    return PRONOUNCE_PATTERN.sub(lambda match: PRONOUNCE[match.group(0)], text)
