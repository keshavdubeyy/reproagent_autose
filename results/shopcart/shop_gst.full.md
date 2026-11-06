# ReproAgent report: `shop_gst`

**Verdict: REPRODUCED**  (mode `full`, 2 attempt(s), 3 SLM calls, 44.9s)

**Fail-to-pass check against fixed version:** PASS (valid reproduction)

## Interpreted bug specification
- Expected: GST should round to Rs 2.21 for a taxable amount of Rs 12.25
- Observed: GST shown is Rs 2.20
- Symbols: GST, gst
- Kind: wrong_value

## Reproduction test
```python
import pytest
from shopcart.pricing import gst
from decimal import Decimal

def test_gst_rounding():
    assert gst(12.25) == Decimal("2.21")
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
______________________________ test_gst_rounding ______________________________
test_reproagent_repro.py:6: in test_gst_rounding
    assert gst(12.25) == Decimal("2.21")
E   AssertionError: assert Decimal('2.2') == Decimal('2.21')
E    +  where Decimal('2.2') = gst(12.25)
E    +  and   Decimal('2.21') = Decimal('2.21')
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_gst_rounding - AssertionError: assert D...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.09s
```

Analyzer judgement: [evidence-match] actual value 2.2 is the wrong value the report observed

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'Decimal' is not defined. Fix imports/names using the module paths and examples shown.
- Attempt 2: run=`failed` exception=`AssertionError` -> **reproduced**
