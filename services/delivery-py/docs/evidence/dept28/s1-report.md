# Fix run report — dlv-run-Q9RAD31PDCEQ1WTSF01CAM859Q

- Service: `toy-py` · Branch: `fix1-toy-py` · Base: `051a6f231eaa129d5be664e881a0a837afcdadb9` (integration-2026-09-24)
- Request: `req-56b1e023122e4c11a6ed` · facts_sha256 `a965f739152ba91a11eea1cf21e31d1e8dab35e2dfaf1993837654bffc732fc5`
- Model: fake / fake-engineer (FAKE, non-production)
- Policy version 1 · prompts manifest `a55a2a0f6ef5979a4ff4df702b126bb3d43d05c208cbae6d10e0da8bc420ee1e`

## Suite

- before (untouched worktree): 2 passed / 1 failed / 0 errors / 0 skips · status ok (junit == collected == summary == exit) · evidence `dlv-ev-400758ec4d20daa09f815a0e9c` · exit 1
- after (last commit): 5 passed / 0 failed / 0 errors / 0 skips · status ok (junit == collected == summary == exit) · evidence `dlv-ev-acaf913e7375befd9747baeeb8` · exit 0
- failing before: tests/test_calc.py::test_add_returns_sum

## Findings

### N1-1 — high — state `fixed` (rounds 1)

- Location: `services/toy-py/src/toy/calc.py:6` · class hint `wrong_operator`
- RED: exit 1 · verdict fail · evidence `dlv-ev-2c1a836395410f3d8fb8a600ef` · argv `pytest -q -p no:cacheprovider -rfE -c /mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/engine-c99366e1411b.ini --rootdir=/mnt/user-data/workspace/services/toy-py -o addopts= -o python_files=test_*.py *_test.py -o testpaths=/mnt/user-data/workspace/services/toy-py/tests -o pythonpath=/mnt/user-data/workspace/services/toy-py/src --junitxml=/mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/run-a8cd2c6953fc.xml tests/test_fix_n1_1.py::test_add_sum`

```text
F
=================================== FAILURES ===================================
_________________________________ test_add_sum _________________________________

    def test_add_sum():
>       assert calc.add(2, 3) == 5
E       assert -1 == 5
E        +  where -1 = <function add at 0x7f3c4ffd16c0>(2, 3)
E        +    where <function add at 0x7f3c4ffd16c0> = calc.add

tests/test_fix_n1_1.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_fix_n1_1.py::test_add_sum - assert -1 == 5
 +  where -1 = <function add at 0x7f3c4ffd16c0>(2, 3)
 +    where <function add at 0x7f3c4ffd16c0> = calc.add
1 failed in 0.04s
```
- GREEN: exit 0 · verdict pass · evidence `dlv-ev-0e226e633ae5b5f4bb1cc054e1` · argv `pytest -q -p no:cacheprovider -rfE -c /mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/engine-c99366e1411b.ini --rootdir=/mnt/user-data/workspace/services/toy-py -o addopts= -o python_files=test_*.py *_test.py -o testpaths=/mnt/user-data/workspace/services/toy-py/tests -o pythonpath=/mnt/user-data/workspace/services/toy-py/src --junitxml=/mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/run-b2b646ea6020.xml tests/test_fix_n1_1.py::test_add_sum`

```text
.
1 passed in 0.02s
```
- revert check: exit 1 with the fix reverted (must be != 0; verdict fail) · restored exit 0 (verdict pass) · evidence `dlv-ev-a7eaa4778de83b68d28b736227`
- split-diff verification (base `051a6f231eaa129d5be664e881a0a837afcdadb9`): src `services/toy-py/src/toy/calc.py` · test `services/toy-py/tests/test_fix_n1_1.py` · test-infra none · agent tree pass · verification checkout pass · reverted checkout fail
- fix commit `d1b2d26054399a2ee8f1eb01fecc4eed8916858f` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_1.py`
- sweep (wrong_operator): `src/toy/calc.py:6` · evidence `dlv-ev-a4f8e77e87a03ab9ea0537cdcd`
- agent: turns 2 · tool calls 4 · opaque exec 0 · denies 0 · tokens in/out 70/35 · ledger event `dlv-fnd-7e1d9241b376e596cc38b650da746bdbed8aaf28`

### N1-2 — high — state `fixed` (rounds 1)

- Location: `services/toy-py/src/toy/calc.py:11` · class hint `division_by_zero`
- RED: exit 1 · verdict fail · evidence `dlv-ev-b3662df89d2df425ade2765e74` · argv `pytest -q -p no:cacheprovider -rfE -c /mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/engine-c99366e1411b.ini --rootdir=/mnt/user-data/workspace/services/toy-py -o addopts= -o python_files=test_*.py *_test.py -o testpaths=/mnt/user-data/workspace/services/toy-py/tests -o pythonpath=/mnt/user-data/workspace/services/toy-py/src --junitxml=/mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/run-8d62e54e8171.xml tests/test_fix_n1_2.py::test_percent_zero_whole`

```text
F
=================================== FAILURES ===================================
___________________________ test_percent_zero_whole ____________________________

    def test_percent_zero_whole():
>       assert calc.percent(1, 0) == 0.0
               ^^^^^^^^^^^^^^^^^^

tests/test_fix_n1_2.py:5: 
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ 

part = 1, whole = 0

    def percent(part: float, whole: float) -> float:
        """``part`` as a percentage of ``whole``; a zero ``whole`` is 0.0 (nothing to be a part of)."""
>       return part / whole * 100.0
               ^^^^^^^^^^^^
E       ZeroDivisionError: division by zero

src/toy/calc.py:11: ZeroDivisionError
=========================== short test summary info ============================
FAILED tests/test_fix_n1_2.py::test_percent_zero_whole - ZeroDivisionError: division by zero
1 failed in 0.06s
```
- GREEN: exit 0 · verdict pass · evidence `dlv-ev-ecdce3f42b4cbd8c8ddc0d8f3c` · argv `pytest -q -p no:cacheprovider -rfE -c /mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/engine-c99366e1411b.ini --rootdir=/mnt/user-data/workspace/services/toy-py -o addopts= -o python_files=test_*.py *_test.py -o testpaths=/mnt/user-data/workspace/services/toy-py/tests -o pythonpath=/mnt/user-data/workspace/services/toy-py/src --junitxml=/mnt/user-data/workspace/.dlv-engine/c8d7ab70319f923f/run-91343cf344a9.xml tests/test_fix_n1_2.py::test_percent_zero_whole`

```text
.
1 passed in 0.01s
```
- revert check: exit 1 with the fix reverted (must be != 0; verdict fail) · restored exit 0 (verdict pass) · evidence `dlv-ev-42bb319ec867fd13868b9fe390`
- split-diff verification (base `d1b2d26054399a2ee8f1eb01fecc4eed8916858f`): src `services/toy-py/src/toy/calc.py` · test `services/toy-py/tests/test_fix_n1_2.py` · test-infra none · agent tree pass · verification checkout pass · reverted checkout fail
- fix commit `a16c816b19aea79f6c930e83fafc5240deb220d1` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_2.py`
- sweep (division_by_zero): `src/toy/calc.py:11` · evidence `dlv-ev-02df61509ebf9d861bd3787839`
- agent: turns 2 · tool calls 4 · opaque exec 0 · denies 0 · tokens in/out 70/35 · ledger event `dlv-fnd-875969bb36c3bc77ada7ef3567adde0ea905fcf1`

## Blocked

- none

## Disproved (reviewer: re-run the reproduction argv on the base sha)

- none

## New defects noticed (suite failures not in the findings list)

- none

## Commits

- `d1b2d26054399a2ee8f1eb01fecc4eed8916858f` message sha256 `944dcf2eb51d14ff57b8ecb3677eda06c275b4e43e8c4d4da74ec371f5f970c6` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_1.py`
- `a16c816b19aea79f6c930e83fafc5240deb220d1` message sha256 `595e9ba47eeae88744012f8edc64aa7f83121ab9b84f58a6a9b7528e9c316362` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_2.py`

## Evidence

- `dlv-ev-400758ec4d20daa09f815a0e9c` suite_output sha256 `400758ec4d20daa09f815a0e9c3920a8bd99e33654fa5d414dfde5769bbc32a7` (706 bytes)
- `dlv-ev-b86e5bd6fa5635f0d0719a44a6` prompt_manifest sha256 `b86e5bd6fa5635f0d0719a44a61a69d439f7efd7bfb80d2a12453db99b7045c8` (42509 bytes)
- `dlv-ev-3472dd1ef159df3690fc27f017` brief sha256 `3472dd1ef159df3690fc27f017aaa7a0fb265f90dc184988624017bcb249f697` (1718 bytes)
- `dlv-ev-2c1a836395410f3d8fb8a600ef` test_output sha256 `2c1a836395410f3d8fb8a600ef1b6a1519ae7d681703086e7f2d85a3276692a6` (685 bytes)
- `dlv-ev-0e226e633ae5b5f4bb1cc054e1` test_output sha256 `0e226e633ae5b5f4bb1cc054e16669d44f9686fa57a0de94f53d9ad680293f91` (20 bytes)
- `dlv-ev-a7eaa4778de83b68d28b736227` test_output sha256 `a7eaa4778de83b68d28b73622700578dab7b686bd7e608eca0cb9e0e4902ff45` (772 bytes)
- `dlv-ev-a4f8e77e87a03ab9ea0537cdcd` diff sha256 `a4f8e77e87a03ab9ea0537cdcd9072bfbd76d8677c0ed704523c7d6a3b7eada8` (358 bytes)
- `dlv-ev-9ad6b6f52a0d8b54edc079b525` suite_output sha256 `9ad6b6f52a0d8b54edc079b525b6151ef8f49d72a5b8206af5a44c55d2869279` (23 bytes)
- `dlv-ev-8eaa85cd02ead57517c464ee6c` diff sha256 `8eaa85cd02ead57517c464ee6c0a88c07248bde56553f126c084b7d793be6bb6` (649 bytes)
- `dlv-ev-ae445f33ecc0dabc7d871961a5` brief sha256 `ae445f33ecc0dabc7d871961a58ee1dff5e54751f8096f604645513e3748a2fd` (1709 bytes)
- `dlv-ev-b3662df89d2df425ade2765e74` test_output sha256 `b3662df89d2df425ade2765e742df1f44b43b0300e278babc38888fa913ae9c3` (902 bytes)
- `dlv-ev-ecdce3f42b4cbd8c8ddc0d8f3c` test_output sha256 `ecdce3f42b4cbd8c8ddc0d8f3cdf59b57ec43301ac109b9b6f654e301a4556b7` (20 bytes)
- `dlv-ev-42bb319ec867fd13868b9fe390` test_output sha256 `42bb319ec867fd13868b9fe390b08e11583c5644538456523440514e43e0d2c8` (989 bytes)
- `dlv-ev-02df61509ebf9d861bd3787839` diff sha256 `02df61509ebf9d861bd378783914d5407b190a48c51c0853d91476b4ad684f48` (463 bytes)
- `dlv-ev-acaf913e7375befd9747baeeb8` suite_output sha256 `acaf913e7375befd9747baeeb88404e7bd323689d970b3bbc0c95de4aa5abfb2` (24 bytes)
- `dlv-ev-c3247a508276a0e2579fcd46be` diff sha256 `c3247a508276a0e2579fcd46bedcbd8e1d808bfe039b091b94dfe88202ee0925` (771 bytes)
