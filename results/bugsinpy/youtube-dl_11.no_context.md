# ReproAgent report: `youtube-dl_11`

**Verdict: REPRODUCED**  (mode `no_context`, 3 attempt(s), 4 SLM calls, 71.5s)

**Fail-to-pass check against fixed version:** PASS (valid reproduction)

## Interpreted bug specification
- Expected: str_to_int(523) == 523
- Observed: str_to_int(523) raises a TypeError
- Symbols: str_to_int
- Kind: exception

## Reproduction test
```python
import sys
sys.path.append('/path/to/your/project')  # Adjust the path as necessary
from youtube_dl.utils import str_to_int
import pytest

def test_str_to_int_crash():
    # Assert that str_to_int(523) returns 523
    assert str_to_int(523) == 523
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
____________________________ test_str_to_int_crash ____________________________
test_reproagent_repro.py:8: in test_str_to_int_crash
    assert str_to_int(523) == 523
           ^^^^^^^^^^^^^^^
youtube_dl\utils.py:3524: in str_to_int
    int_str = re.sub(r'[,\.\+]', '', int_str)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
C:\Program Files\Python314\Lib\re\__init__.py:208: in sub
    return _compile(pattern, flags).sub(repl, string, count)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   TypeError: expected string or bytes-like object, got 'int'
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_str_to_int_crash - TypeError: expected ...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 1.72s
```

Analyzer judgement: [evidence-match] project raised TypeError, as the report describes

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'pytest' is not defined. Fix imports/names using the module paths and examples shown.
- Attempt 2: run=`passed` exception=`None` -> **test_passed** — The test PASSED, so it did not trigger the bug. Use the exact example input from the report and assert the EXPECTED value stated in the report.
- Attempt 3: run=`failed` exception=`TypeError` -> **reproduced**
