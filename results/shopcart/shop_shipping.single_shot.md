# ReproAgent report: `shop_shipping`

**Verdict: NOT REPRODUCED**  (mode `single_shot`, 1 attempt(s), 2 SLM calls, 45.8s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: Free shipping for orders of Rs 500 or more
- Observed: Rs 50 shipping fee charged for an Rs 500 order
- Symbols: shipping_fee, free_shipping, order_amount
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`failed` exception=`AttributeError` -> **invalid_test** — The exception was raised by the test code itself, not by the project: AttributeError: 'Cart' object has no attribute 'add_item'. Check the function signature and how existing tests call it.
