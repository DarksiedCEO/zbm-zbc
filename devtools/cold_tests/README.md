# Focused cold restart/archive/operator tests

Run from the repository root:

```sh
python3 -B -m unittest discover -s devtools/cold_tests -p 'test_*.py'
```

These tests import only `deploy/docker/maintenance/cold` through a repository-relative bootstrap. No private predecessor path, Docker, application startup or provider invocation is used. Temporary directories are disposable test state. `test_typed_cleanup.py` and `test_typed_cleanup_coordinator.py` retain the existing fixtures imported by the repaired-disposition tests; their assertions are unchanged.

Builder integration only, not independent qualification.
