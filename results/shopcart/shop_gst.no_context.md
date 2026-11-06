# ReproAgent report: `shop_gst`

**Verdict: REPRODUCED**  (mode `no_context`, 1 attempt(s), 2 SLM calls, 35.4s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: GST should round to Rs 2.21 for a taxable amount of Rs 12.25
- Observed: GST shown is Rs 2.20
- Symbols: GST, gst
- Kind: wrong_value

## Reproduction test
```python
import shopcart.pricing

def test_gst_rounding():
    # Given a taxable amount of Rs 12.25
    taxable_amount = 12.25
    
    # Calculate the GST
    gst = shopcart.pricing.gst(taxable_amount)
    
    # Expected GST should round to Rs 2.21
    expected_gst = round(12.25 * 0.18, 2)
    
    # Assert the calculated GST matches the expected GST
    assert gst == expected_gst, f"Expected GST to be Rs {expected_gst}, but got Rs {gst}"
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
______________________________ test_gst_rounding ______________________________
test_reproagent_repro.py:14: in test_gst_rounding
    assert gst == expected_gst, f"Expected GST to be Rs {expected_gst}, but got Rs {gst}"
E   AssertionError: Expected GST to be Rs 2.21, but got Rs 2.2
E   assert Decimal('2.2') == 2.21
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_gst_rounding - AssertionError: Expected...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.10s
```

Analyzer judgement: [evidence-match] actual value 2.2 is the wrong value the report observed

## Attempt log
- Attempt 1: run=`failed` exception=`AssertionError` -> **reproduced**
