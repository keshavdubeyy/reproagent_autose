# ReproAgent report: `youtube-dl_11`

**Verdict: REPRODUCED**  (mode `full_v2`, 1 attempt(s), 2 SLM calls, 36.7s)

**Fail-to-pass check against fixed version:** PASS (valid reproduction)

## Interpreted bug specification
- Expected: str_to_int(523) == 523
- Observed: str_to_int(523) raises a TypeError
- Symbols: str_to_int
- Kind: exception

## Reproduction test
```python
import unittest
from youtube_dl.utils import str_to_int

class TestStrToInt(unittest.TestCase):
    def test_str_to_int_with_int(self):
        self.assertEqual(str_to_int(523), 523)

if __name__ == '__main__':
    unittest.main()
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
____________________ TestStrToInt.test_str_to_int_with_int ____________________
test_reproagent_repro.py:6: in test_str_to_int_with_int
    self.assertEqual(str_to_int(523), 523)
                     ^^^^^^^^^^^^^^^
youtube_dl\utils.py:3524: in str_to_int
    int_str = re.sub(r'[,\.\+]', '', int_str)
              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
C:\Program Files\Python314\Lib\re\__init__.py:208: in sub
    return _compile(pattern, flags).sub(repl, string, count)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E   TypeError: expected string or bytes-like object, got 'int'
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::TestStrToInt::test_str_to_int_with_int - Typ...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 7.68s
```

Analyzer judgement: [evidence-match] project raised TypeError, as the report describes

## Attempt log
- Attempt 1: run=`failed` exception=`TypeError` -> **reproduced**
