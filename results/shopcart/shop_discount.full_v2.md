# ReproAgent report: `shop_discount`

**Verdict: NOT REPRODUCED**  (mode `full_v2`, 4 attempt(s), 6 SLM calls, 184.4s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: orders below Rs 1,000 should not receive a discount; the discount should be 0 and the amount before tax should remain Rs 800
- Observed: the application applies a 10% discount (Rs 80) and the discounted amount is Rs 720
- Symbols: calculate_discount, apply_discount, order_total, discount
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`failed` exception=`AssertionError` -> **wrong_failure** — The test failed, but the report observed 10, 720, 80 but your test produced 206, so your inputs do not match the report's scenario. Build exactly the report's scenario and assert: orders below Rs 1,00
- Attempt 2: run=`failed` exception=`AssertionError` -> **wrong_failure** — The test failed, but the report observed 10, 720, 80 but your test produced 86, so your inputs do not match the report's scenario. Build exactly the report's scenario and assert: orders below Rs 1,000
- Attempt 3: run=`failed` exception=`AssertionError` -> **wrong_failure** — The test failed, but the report observed 10, 720, 80 but your test produced 86, so your inputs do not match the report's scenario. Build exactly the report's scenario and assert: orders below Rs 1,000
- Attempt 4: run=`failed` exception=`AssertionError` -> **wrong_failure** — The test failed, but the report observed 10, 720, 80 but your test produced 86, so your inputs do not match the report's scenario. Build exactly the report's scenario and assert: orders below Rs 1,000
