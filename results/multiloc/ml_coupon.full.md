# ReproAgent report: `ml_coupon`

**Verdict: REPRODUCED**  (mode `full`, 2 attempt(s), 3 SLM calls, 78.2s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: {"cart.apply_coupon(' save10 ')": 'True', 'A Rs 2,000 order with coupon SAVE10': 'Rs 180'}
- Observed: {"cart.apply_coupon(' save10 ')": 'False', 'A Rs 2,000 order with coupon SAVE10': 'Rs 200'}
- Symbols: cart, apply_coupon, SAVE10, True, order
- Kind: wrong_value

## Reproduction test
```python
import pytest
from shopcart import Cart, Product, checkout
from shopcart.pricing import gst, money

def test_apply_coupon_with_spaces():
    cart = Cart()
    assert cart.apply_coupon(' save10 ') == False, "Expected True for 'save10 '"

def test_coupon_computed_on_wrong_amount():
    cart = Cart()
    product = Product(sku='prod1', name='Product 1', price=2000)
    cart.add(product, 1)
    cart.apply_coupon('SAVE10')
    assert cart.subtotal() == money(1800), "Expected subtotal of Rs 1800"
```

## Evidence (pytest output on the buggy version)
```
.F
================================== FAILURES ===================================
____________________ test_coupon_computed_on_wrong_amount _____________________
test_reproagent_repro.py:14: in test_coupon_computed_on_wrong_amount
    assert cart.subtotal() == money(1800), "Expected subtotal of Rs 1800"
E   AssertionError: Expected subtotal of Rs 1800
E   assert Decimal('2000.00') == Decimal('1800.00')
E    +  where Decimal('2000.00') = subtotal()
E    +    where subtotal = <shopcart.cart.Cart object at 0x000001D849296850>.subtotal
E    +  and   Decimal('1800.00') = money(1800)
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_coupon_computed_on_wrong_amount - Asser...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 1 passed in 0.09s
```

Analyzer judgement: [evidence-match] actual value 2000 is the wrong value the report observed

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'Decimal' is not defined. Fix imports/names using the module paths and examples shown.
- Attempt 2: run=`failed` exception=`AssertionError` -> **reproduced**
