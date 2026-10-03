"""Fix wave 26b (CI #3, delivery-py 3.13 macos-26): the argv-level Docker double runs the adapter's commands on the
host, and on a host without GNU coreutils/findutils (macOS) the two GNU-only spellings the adapter emits failed —
``mv -f -T`` (every ``put_bytes``: the engine's ini write, every file-tool write) and ``find … -printf '%p\\n'``
(``ls``) — so every harness run ended in HARNESS_ERROR "write failed (mv)" before the model's first call, and the
adversarial tests saw no tool decisions at all. The real sandbox is Debian (GNU tools), so production never saw it.
The double now gives those two spellings their GNU meaning on any host; these tests pin that meaning."""

from __future__ import annotations

import os

from fakes import FakeDockerCli

WS = "/mnt/user-data/workspace"


def _box(tmp_path):
    d = FakeDockerCli(str(tmp_path / "docker"))
    assert d.run(["run", "--name", "dlv-g", "img"], timeout_s=30).exit_code == 0
    vol = d.containers["dlv-g"]
    return d, vol


def _exec(d, *cmd):
    return d.run(["exec", "dlv-g", *cmd], timeout_s=30)


def test_the_double_runs_gnu_mv_no_target_directory_on_any_host(tmp_path):
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "stage"))
    with open(os.path.join(vol, "stage", "f"), "w") as fh:
        fh.write("new")
    with open(os.path.join(vol, "dest"), "w") as fh:
        fh.write("old")
    r = _exec(d, "mv", "-f", "-T", "--", f"{WS}/stage/f", f"{WS}/dest")
    assert r.exit_code == 0, r
    assert open(os.path.join(vol, "dest")).read() == "new"
    assert not os.path.exists(os.path.join(vol, "stage", "f"))


def test_the_doubles_mv_t_never_moves_into_a_directory(tmp_path):
    # GNU -T: the destination is the name itself, never "into" it — a directory there (an R7 swap) is refused
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "stage"))
    with open(os.path.join(vol, "stage", "f"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(vol, "dest"))
    r = _exec(d, "mv", "-f", "-T", "--", f"{WS}/stage/f", f"{WS}/dest")
    assert r.exit_code != 0, r
    assert os.listdir(os.path.join(vol, "dest")) == []
    assert os.path.exists(os.path.join(vol, "stage", "f"))


def test_the_double_runs_gnu_find_printf_path_on_any_host(tmp_path):
    d, vol = _box(tmp_path)
    os.makedirs(os.path.join(vol, "a", "b"))
    open(os.path.join(vol, "a", "f.txt"), "w").close()
    r = _exec(d, "find", f"{WS}/a", "-maxdepth", "1", "-mindepth", "1", "-printf", "%p\n")
    assert r.exit_code == 0, r
    assert sorted(r.stdout.decode().splitlines()) == [f"{WS}/a/b", f"{WS}/a/f.txt"]


# ====================================================================== CI #3 macos-26: the service could not start
def test_macos_corefoundation_encoding_name_is_allowed_on_darwin_only_and_only_in_its_shape():
    """CPython on macOS gets ``__CF_USER_TEXT_ENCODING`` written into its own environment by CoreFoundation at start
    (``env -i python -c 'import os; print(sorted(os.environ))'`` → ``['LC_CTYPE', '__CF_USER_TEXT_ENCODING']``), so the
    launcher refused every start on macOS (CI #3: four L2 errors, two failures). Same class as LC_CTYPE (written by
    CPython's PEP 538 coercion): allowed — but on darwin only, and only as the three-hex-field value CF writes."""
    from zbm_delivery import config as C
    name = "__CF_USER_TEXT_ENCODING"
    assert C.env_problems({name: "0x1F5:0x0:0x0"}, platform="darwin") == []
    assert C.env_problems({name: "0x1F5:0x0:0x0"}, platform="linux") == [name]
    for bad in ("", "x", "0x1F5:0x0", "0x1F5:0x0:0x0;rm", "0x1F5:0x0:0x0\n", "1F5:0:0"):
        assert C.env_problems({name: bad}, platform="darwin") == [f"{name} (value is not CoreFoundation's 0xH:0xH:0xH)"], bad


def test_env_problems_defaults_to_this_platform():
    import sys
    from zbm_delivery import config as C
    assert C.env_problems({"__CF_USER_TEXT_ENCODING": "0x1F5:0x0:0x0"}) == ([] if sys.platform == "darwin" else ["__CF_USER_TEXT_ENCODING"])

