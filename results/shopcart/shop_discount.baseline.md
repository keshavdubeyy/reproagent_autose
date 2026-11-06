# ReproAgent report: `shop_discount`

**Verdict: NOT REPRODUCED**  (mode `baseline`, 1 attempt(s), 2 SLM calls, 54.4s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: orders below Rs 1,000 should not receive a discount; the discount should be 0 and the amount before tax should remain Rs 800
- Observed: the application applies a 10% discount (Rs 80) and the discounted amount is Rs 720
- Symbols: calculate_discount, apply_discount, order_total, discount
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`collection_error` exception=`ModuleNotFoundError` -> **invalid_test** — The test could not run because of a mistake in the test itself: ModuleNotFoundError: No module named 'your_module'. Fix imports/names using the module paths and examples shown.
