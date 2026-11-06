# ReproAgent report: `youtube-dl_17`

**Verdict: NOT REPRODUCED**  (mode `single_shot`, 1 attempt(s), 2 SLM calls, 42.8s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: The function should return an empty list when the param key is missing from the dict.
- Observed: The function raises an exception when the param key is missing from the dict.
- Symbols: cli_bool_option, params, command_option, param
- Kind: exception

## Attempt log
- Attempt 1: run=`passed` exception=`None` -> **test_passed** — The test PASSED, so it did not trigger the bug. Use the exact example input from the report and assert the EXPECTED value stated in the report.
