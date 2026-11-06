# ReproAgent report: `shop_gst`

**Verdict: NOT REPRODUCED**  (mode `single_shot`, 1 attempt(s), 2 SLM calls, 29.5s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: GST should round to Rs 2.21 for a taxable amount of Rs 12.25
- Observed: GST shown is Rs 2.20
- Symbols: GST, gst
- Kind: wrong_value

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'Decimal' is not defined. Fix imports/names using the module paths and examples shown.
