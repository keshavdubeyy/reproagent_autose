# ReproAgent report: `shop_coupon`

**Verdict: NOT REPRODUCED**  (mode `single_shot`, 1 attempt(s), 2 SLM calls, 40.7s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: Coupon codes should be case-insensitive, allowing 'save10' to be accepted.
- Observed: Cart.apply_coupon('save10') returns False, while Cart.apply_coupon('SAVE10') returns True.
- Symbols: Cart, apply_coupon, rejected
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`passed` exception=`None` -> **test_passed** — The test PASSED, so it did not trigger the bug. Use the exact example input from the report and assert the EXPECTED value stated in the report.
