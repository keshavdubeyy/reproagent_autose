# ReproAgent report: `youtube-dl_3`

**Verdict: NOT REPRODUCED**  (mode `baseline`, 1 attempt(s), 2 SLM calls, 34.7s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: The function should decode HTML entities that come after a bare '&' that is not part of an entity.
- Observed: The function does not decode HTML entities that come after a bare '&' that is not part of an entity.
- Symbols: unescapeHTML
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`passed` exception=`None` -> **test_passed** — The test PASSED, so it did not trigger the bug. Use the exact example input from the report and assert the EXPECTED value stated in the report.
