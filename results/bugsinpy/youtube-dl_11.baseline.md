# ReproAgent report: `youtube-dl_11`

**Verdict: NOT REPRODUCED**  (mode `baseline`, 1 attempt(s), 2 SLM calls, 31.4s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: str_to_int(523) == 523
- Observed: str_to_int(523) raises a TypeError
- Symbols: str_to_int
- Kind: exception

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'pytest' is not defined. Fix imports/names using the module paths and examples shown.
