"""AEGIS sweep A (on 5d49ee9): the opt-out normaliser shared with sales-py — "_" separates words, repeated letters
collapse, "unsub" is an opt-out. Failed on 5d49ee9."""

from intelligences import i07_replies


def test_underscore_repeated_letters_and_unsub_are_opt_outs():
    for t in ("please_unsubscribe", "STOP_please", "S_T_O_P", "unsub", "stoooop", "UNSUBBBSCRIBE"):
        for ch in ("email", "instagram"):
            assert i07_replies.classify(t, ch) == "unsubscribe", (t, ch)
    assert i07_replies.classify("hello there", "email") == "review"


def test_l1_accepted_false_positives_are_pinned():
    """AEGIS L1 (accepted): over-matching on the honouring side. Collapsing repeated letters reads "stoop" as
    "stop", and unsub\\w* matches "unsubtle"; both are labelled opt-outs (a person can lift the result). A change
    to either behaviour must be deliberate: these pin it."""
    for t in ("I'll be on the stoop at noon", "that was an unsubtle hint", "steeeeppp stoooop"):
        assert i07_replies.classify(t, "email") == "unsubscribe", t
    for t in ("my stopwatch broke", "unsure about the timing", "subscribe me please"):
        assert i07_replies.classify(t, "email") != "unsubscribe", t
