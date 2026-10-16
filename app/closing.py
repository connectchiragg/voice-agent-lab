"""Ending a call and opening one, decided from the caller's words without the LLM.

- `closing(text)`: "strong" for an unmistakable goodbye ("bye", "you can end the call",
  "that's all, thanks"): the call answers with a fixed goodbye and hangs up, even when
  the LLM is unavailable. "soft" for a likely one ("that's all", "I'm good"): the LLM
  decides (it has `end_call`), unless it fails; then it is taken as a goodbye too.
  None otherwise. Questions and negations ("don't hang up") are never closings.
- `only_hello(text)`: "Hello?" and the like, said when someone picks up the phone.
- `request_end` / `take_end_request`: the `end_call` tool asks the call to hang up
  once the reply it belongs to has been spoken.
"""
import re
import zlib

GOODBYES = ("Alright, talk soon. Bye!", "Okay, take care. Bye!", "Sounds good. Bye for now!")
STILL_THERE = "Still there?"
IDLE_GOODBYE = "I'll let you go. Talk soon, bye!"

# Said anywhere in a short turn, these end the call.
STRONG = re.compile(r"\b(end|finish|close|stop) (the|this) call\b|\bhang up\b|\bgood ?bye\b|"
                    r"\bbye\b|\btalk (to you )?(later|soon)\b|"
                    r"\b(that ?s|that is) (all|it|everything)( for (now|today))?,? thanks?\b|"
                    r"\bthanks?,? (that ?s|that is) (all|it|everything)\b|"
                    r"\b(that ?ll|that will) be all\b")
# The whole turn, once leading and trailing filler is removed.
SOFT = {"thats all", "thats it", "thats everything", "thats all for now", "thats it for now",
        "im good", "i think im good", "im all set", "all set", "im done", "were done",
        "nothing else", "thats all i needed", "i think thats all", "i think thats it",
        "that is all", "that is it", "no thats all", "no thats it", "nope thats all"}
FILLER = {"ok", "okay", "cool", "alright", "right", "and", "so", "yeah", "yes", "great",
          "perfect", "thanks", "thank", "you", "awesome", "nice", "well", "um", "uh", "oh"}
NEGATION = re.compile(r"\b(don ?t|do not|not|never|wait|no no)\b")
HELLO = {"hello", "hi", "hey", "hiya", "yes", "yeah", "yep", "speaking", "there", "hallo", "helo"}
MAX_CLOSING_WORDS = 12


def words(text: str) -> list[str]:
    """ "That's all, thanks!" -> ["thats", "all", "thanks"]."""
    return re.sub(r"[^\w\s]", "", re.sub(r"[-–—]", " ", text.lower().replace("’", "'"))).split()


def closing(text: str) -> str | None:
    said = words(text)
    if not said or len(said) > MAX_CLOSING_WORDS or "?" in text:
        return None
    joined = " ".join(said)
    if NEGATION.search(joined):
        return None
    if STRONG.search(joined):
        return "strong"
    while said and said[0] in FILLER:
        said = said[1:]
    while said and said[-1] in FILLER:
        said = said[:-1]
    return "soft" if " ".join(said) in SOFT else None


def only_hello(text: str) -> bool:
    """A pickup greeting ("Hello?", "Hi, yes?", "Hello hello"): no request in it."""
    said = words(text)
    return bool(said) and len(said) <= 4 and all(word in HELLO for word in said) \
        and any(word in ("hello", "hi", "hey", "hiya", "hallo", "helo") for word in said)


def goodbye_line(conversation_id: str) -> str:
    """One of a few goodbyes, fixed per call (not the same words on every call)."""
    return GOODBYES[zlib.crc32(conversation_id.encode()) % len(GOODBYES)]


end_requests: set[str] = set()  # Conversation ids whose `end_call` awaits the reply's end.


def request_end(conversation_id: str) -> None:
    end_requests.add(conversation_id)


def take_end_request(conversation_id: str) -> bool:
    if conversation_id in end_requests:
        end_requests.discard(conversation_id)
        return True
    return False
