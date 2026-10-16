"""Goodbyes and pickup greetings, recognised without the LLM."""
import pytest

from app import closing


@pytest.mark.parametrize("text", [
    "You can end the call.", "Okay, bye!", "Thanks, that's all.", "That's it, thanks.",
    "Alright, talk to you later.", "Please hang up.", "Goodbye", "Okay cool, bye bye.",
    "That'll be all."])
def test_clear_goodbyes_are_strong(text):
    assert closing.closing(text) == "strong"


@pytest.mark.parametrize("text", [
    "And that's all.", "No, that's it.", "Okay cool, I think I'm good.", "I'm all set, thanks",
    "Nothing else."])
def test_likely_goodbyes_are_soft(text):
    assert closing.closing(text) == "soft"


@pytest.mark.parametrize("text", [
    "Don't hang up yet.", "That's all?", "Is that all you found?", "Tell me about the pay.",
    "Wait, that's not all.", "", "I said goodbye to my last job and now I want something "
    "bigger with more scope and a better team, honestly"])
def test_everything_else_is_not_a_goodbye(text):
    assert closing.closing(text) is None


def test_pickup_hellos():
    assert all(closing.only_hello(t) for t in ("Hello?", "Hi, yes?", "Hello hello", "Hey"))
    assert not any(closing.only_hello(t) for t in ("Yes", "Hello, can you call later?", "", "Okay"))


def test_end_requests_are_taken_once():
    closing.request_end("c1")
    assert closing.take_end_request("c1") and not closing.take_end_request("c1")


def test_goodbye_line_is_fixed_per_call():
    assert closing.goodbye_line("a") == closing.goodbye_line("a") in closing.GOODBYES
