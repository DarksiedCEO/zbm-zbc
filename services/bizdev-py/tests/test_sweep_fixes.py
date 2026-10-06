"""AEGIS sweep A (on 5d49ee9): the opt-out normaliser shared with sales-py — "_" separates words, repeated letters
collapse, "unsub" is an opt-out. Failed on 5d49ee9."""

from intelligences import i09_replies


def test_underscore_repeated_letters_and_unsub_are_opt_outs():
    for t in ("please_unsubscribe", "STOP_please", "S_T_O_P", "unsub", "stoooop", "UNSUBBBSCRIBE"):
        assert i09_replies.classify(t, "email") == "unsubscribe", t
    assert i09_replies.classify("hello there", "email") == "review"
