# ReproAgent report: toy_calc (full)

| field | value |
|---|---|
| reproduction | **reproduced** (valid: True, attempts: 1) |
| fail-to-pass oracle | True |
| fix | **fixed** (attempts: 1, regression-free: True) |
| time | 204.0 s, 20 LLM calls |

## Reproduction test
```python
from calc.stats import median
test_data = [
    ([1, 2, 3, 4], 2.5),
    ([1, 2, 3], 2),
]

def test_median():
    for values, expected in test_data:
        result = median(values)
        assert result == expected, f"Expected {expected}, got {result}"
```

## Evidence (pytest on buggy code)
```
F
=================================== FAILURES ===================================
_________________________________ test_median __________________________________

    def test_median():
        for values, expected in test_data:
            result = median(values)
>           assert result == expected, f"Expected {expected}, got {result}"
E           AssertionError: Expected 2.5, got 3
E           assert 3 == 2.5

tests/test_repro_agent.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_repro_agent.py::test_median - AssertionError: Expected 2.5,...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.02s

```

## Proposed patch
```diff
--- a/calc/stats.py
+++ b/calc/stats.py
@@ -10,4 +10,4 @@
     mid = n // 2
     if n % 2 == 1:
         return s[mid]
-    return s[mid]
+    return (s[mid - 1] + s[mid]) / 2

```

## Agent explanation
The test failed with the expected error, confirming the bug in the median function for even-length lists.
Fix: Fixed the median function to correctly calculate the median for even-length lists. Previously, the code was returning the wrong value for even-length lists. The fix involves sorting the list, calculating the middle index, and averaging the two middle numbers for even-length lists. The code now passes the test cases.
