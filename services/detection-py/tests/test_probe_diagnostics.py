"""Diagnostic availability must never become fictitious scheduler evidence."""
import pytest

import test_request_limits_live as live


def _helpers():
    scope = {"__name__": "probe_helpers"}
    source = live._PROBER.split("me = os.getpid()", 1)[0]
    source = source.replace("port, server_pid, seconds, every = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])", "")
    exec(compile(source, "<probe_helpers>", "exec"), scope)
    return scope


@pytest.mark.parametrize("before,after,expected", [(1.0, 1.25, .25), (None, 1., None), (1., None, None), (None, None, None)])
def test_wait_delta_retains_missing_measurements(before, after, expected):
    assert _helpers()["wait_delta"](before, after) == expected


@pytest.mark.parametrize("prober,server,expected", [(None, None, .6), (.1, None, .5), (None, .2, .4), (.1, .2, .4), (.8, .2, 0.), (None, -.1, .6)])
def test_latency_uses_only_available_larger_wait(prober, server, expected):
    assert live._service_latency({"wall": .6, "prober_wait": prober, "server_wait": server}) == pytest.approx(expected)


def test_missing_summary_is_not_zero_delay():
    assert live._wait_summary([{"server_wait": None}], "server_wait") == "unavailable (0/1 samples)"
    assert live._wait_summary([{"server_wait": None}, {"server_wait": .002}], "server_wait") == "2ms (1/2 samples)"
