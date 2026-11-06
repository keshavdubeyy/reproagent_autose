# ReproAgent report: `shop_shipping`

**Verdict: REPRODUCED**  (mode `full`, 2 attempt(s), 3 SLM calls, 75.2s)

**Fail-to-pass check against fixed version:** FAIL

## Interpreted bug specification
- Expected: Free shipping for orders of Rs 500 or more
- Observed: Rs 50 shipping fee charged for an Rs 500 order
- Symbols: shipping_fee, free_shipping, order_amount
- Kind: wrong_value

## Reproduction test
```python
import pytest
from shopcart import Cart, Product, checkout
from shopcart.pricing import gst, money

def test_free_shipping_for_500():
    cart = Cart()
    cart.add(Product("product1", 100, 10), 1)
    cart.add(Product("product2", 200, 5), 1)
    cart.add(Product("product3", 300, 3), 1)
    cart.add(Product("product4", 400, 2), 1)
    cart.add(Product("product5", 500, 1), 1)

    bill = checkout(cart)
    assert bill["shipping"] == money(0)
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
_________________________ test_free_shipping_for_500 __________________________
test_reproagent_repro.py:14: in test_free_shipping_for_500
    assert bill["shipping"] == money(0)
E   AssertionError: assert Decimal('50.00') == Decimal('0.00')
E    +  where Decimal('0.00') = money(0)
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_free_shipping_for_500 - AssertionError:...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.08s
```

Analyzer judgement: [evidence-match] actual value 50 is the wrong value the report observed

## Attempt log
- Attempt 1: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'money' is not defined. Fix imports/names using the module paths and examples shown.
- Attempt 2: run=`failed` exception=`AssertionError` -> **reproduced**
