# Fix run report — dlv-run-CT9N5DC5KYVQWQKKDJP2NGCE0D

- Service: `toy-py` · Branch: `fix1-toy-py` · Base: `b1e36f9765197529940346f361f307bb479d4002` (integration-2026-09-24)
- Request: `req-f00251ec545142929b9a` · facts_sha256 `4ffd0148c921349f9ffd5bd63d45a3eb0d57b7c8bfe0250112692556f7e798b0`
- Model: fake / fake-engineer (FAKE, non-production)
- Policy version 1 · prompts manifest `064a5e3e3b3ea696cce1a586eef792a9a73e26ed8b45dabe9dfa20fa2d295440`

## Suite

- before (untouched worktree): 2 passed / 1 failed / 0 errors / 0 skips · evidence `dlv-ev-471edebf08719283a6d5649c76` · exit 1
- after (last commit): 5 passed / 0 failed / 0 errors / 0 skips · evidence `dlv-ev-9310d484212a1fe2c33b4b8acc` · exit 0
- failing before: tests/test_calc.py::test_add_returns_sum

## Findings

### N1-1 — high — state `fixed` (rounds 1)

- Location: `services/toy-py/src/toy/calc.py:6` · class hint `wrong_operator`
- RED: exit 1 · evidence `dlv-ev-cd869f49e72e4aa2470ace2129` · argv `pytest -q -p no:cacheprovider tests/test_fix_n1_1.py::test_add_sum`

```text
F                                                                        [100%]
=================================== FAILURES ===================================
_________________________________ test_add_sum _________________________________

    def test_add_sum():
>       assert calc.add(2, 3) == 5
E       assert -1 == 5
E        +  where -1 = <function add at 0x7f7c452d93a0>(2, 3)
E        +    where <function add at 0x7f7c452d93a0> = calc.add

tests/test_fix_n1_1.py:5: AssertionError
=========================== short test summary info ============================
FAILED tests/test_fix_n1_1.py::test_add_sum - assert -1 == 5
 +  where -1 = <function add at 0x7f7c452d93a0>(2, 3)
 +    where <function add at 0x7f7c452d93a0> = calc.add
1 failed in 0.03s
```
- GREEN: exit 0 · evidence `dlv-ev-4bcefcc9f6c645923b9993ccf7` · argv `pytest -q -p no:cacheprovider tests/test_fix_n1_1.py::test_add_sum`

```text
.                                                                        [100%]
1 passed in 0.01s
```
- revert check: exit 1 with the fix reverted (must be != 0) · restored exit 0 · evidence `dlv-ev-b6df622a9a45aa1c0a8215a676`
- fix commit `546af22f4a98150eb652ead40671762ae5b4580c` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_1.py`
- sweep (wrong_operator): `src/toy/calc.py:6` · evidence `dlv-ev-a4f8e77e87a03ab9ea0537cdcd`
- agent: turns 2 · tool calls 4 · tokens in/out 70/35

### N1-2 — high — state `fixed` (rounds 1)

- Location: `services/toy-py/src/toy/calc.py:11` · class hint `division_by_zero`
- RED: exit 1 · evidence `dlv-ev-96cea0496bf7a7c7ef483b0d98` · argv `pytest -q -p no:cacheprovider tests/test_fix_n1_2.py::test_percent_zero_whole`

```text
F                                                                        [100%]
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
1 failed in 0.03s
```
- GREEN: exit 0 · evidence `dlv-ev-4bcefcc9f6c645923b9993ccf7` · argv `pytest -q -p no:cacheprovider tests/test_fix_n1_2.py::test_percent_zero_whole`

```text
.                                                                        [100%]
1 passed in 0.01s
```
- revert check: exit 1 with the fix reverted (must be != 0) · restored exit 0 · evidence `dlv-ev-aacfd7d7ef86b0eb4101b068f7`
- fix commit `31729adbdecf02a7da06dfcd083bb973af6a8886` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_2.py`
- sweep (division_by_zero): `src/toy/calc.py:11` · evidence `dlv-ev-02df61509ebf9d861bd3787839`
- agent: turns 2 · tool calls 4 · tokens in/out 70/35

## Blocked / disproved

- none

## New defects noticed (suite failures not in the findings list)

- none

## Commits

- `546af22f4a98150eb652ead40671762ae5b4580c` message sha256 `944dcf2eb51d14ff57b8ecb3677eda06c275b4e43e8c4d4da74ec371f5f970c6` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_1.py`
- `31729adbdecf02a7da06dfcd083bb973af6a8886` message sha256 `595e9ba47eeae88744012f8edc64aa7f83121ab9b84f58a6a9b7528e9c316362` · files: `services/toy-py/src/toy/calc.py`, `services/toy-py/tests/test_fix_n1_2.py`

## Evidence

- `dlv-ev-471edebf08719283a6d5649c76` suite_output sha256 `471edebf08719283a6d5649c765fe5070b22ac015f1e0219991c3e5b573e47df` (782 bytes)
- `dlv-ev-b89f789b8a68bcd67a4a0a443e` prompt_manifest sha256 `b89f789b8a68bcd67a4a0a443ec1870ec2bbf0aca1cf19d73ea0fa8c0995f4c4` (41851 bytes)
- `dlv-ev-992212ffa51ca2d35360ef03bb` brief sha256 `992212ffa51ca2d35360ef03bbe59e249c072517b4a00f3b4dc23f3db33bb3bc` (1708 bytes)
- `dlv-ev-cd869f49e72e4aa2470ace2129` test_output sha256 `cd869f49e72e4aa2470ace2129dfffbee3ea4dd3e623dbeeb29586f7e8a9d9e4` (763 bytes)
- `dlv-ev-4bcefcc9f6c645923b9993ccf7` test_output sha256 `4bcefcc9f6c645923b9993ccf73ad92e6b400ca25458ea14e095857fc2131342` (98 bytes)
- `dlv-ev-b6df622a9a45aa1c0a8215a676` test_output sha256 `b6df622a9a45aa1c0a8215a676ae516e306aa63c82046a1741ffa56963f2a159` (879 bytes)
- `dlv-ev-a4f8e77e87a03ab9ea0537cdcd` diff sha256 `a4f8e77e87a03ab9ea0537cdcd9072bfbd76d8677c0ed704523c7d6a3b7eada8` (358 bytes)
- `dlv-ev-4250d1f2029068ddc50a143f43` suite_output sha256 `4250d1f2029068ddc50a143f438d48c0e151e05c734fe713ffea94436ecf9381` (98 bytes)
- `dlv-ev-8eaa85cd02ead57517c464ee6c` diff sha256 `8eaa85cd02ead57517c464ee6c0a88c07248bde56553f126c084b7d793be6bb6` (649 bytes)
- `dlv-ev-8ba69cc6bffc07bb0aaaed4253` brief sha256 `8ba69cc6bffc07bb0aaaed42534a420224be585bf319f46f23ea4b5ee0821c0e` (1699 bytes)
- `dlv-ev-96cea0496bf7a7c7ef483b0d98` test_output sha256 `96cea0496bf7a7c7ef483b0d98ebfac4fdb69af3a2fff16df84e7de453ebb4b4` (980 bytes)
- `dlv-ev-aacfd7d7ef86b0eb4101b068f7` test_output sha256 `aacfd7d7ef86b0eb4101b068f717c2928bbf780aa3a37ee39aa6d949ae2b21f5` (1096 bytes)
- `dlv-ev-02df61509ebf9d861bd3787839` diff sha256 `02df61509ebf9d861bd378783914d5407b190a48c51c0853d91476b4ad684f48` (463 bytes)
- `dlv-ev-9310d484212a1fe2c33b4b8acc` suite_output sha256 `9310d484212a1fe2c33b4b8accdf50a2639f09ba039354f8448f8aa8d173e1ff` (98 bytes)
- `dlv-ev-c3247a508276a0e2579fcd46be` diff sha256 `c3247a508276a0e2579fcd46bedcbd8e1d808bfe039b091b94dfe88202ee0925` (771 bytes)
