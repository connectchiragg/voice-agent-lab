from app.briefing_quality import concise_speech, useful_fact


def test_unverified_people_and_empty_recordings_do_not_take_briefing_space():
    assert not useful_fact("A profile does not confirm she is the interviewer.", "interviewer")
    assert not useful_fact("The listing holds no transcript or discussion.", "previous_round")
    assert useful_fact("Last round asked how you recover a partially completed action.", "previous_round")
    assert useful_fact("No public funding or role pay was found in the checked sources.", "finance")
    assert useful_fact("The interviewer built a messaging service.", "interviewer")


def test_spoken_brief_does_not_narrate_missing_research():
    text = ("Acme builds an assistant. The earlier recap holds no transcript or feedback. "
            "Interviewer backgrounds are not verified, so ask them. Prepare a failure recovery example.")
    assert concise_speech(text) == "Acme builds an assistant. Prepare a failure recovery example."
