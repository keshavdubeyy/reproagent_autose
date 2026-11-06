# ReproAgent report: `shop_shipping`

**Verdict: REPRODUCED**  (mode `full_v2`, 3 attempt(s), 4 SLM calls, 87.8s)

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
from shopcart.pricing import money, SHIPPING_FEE, FREE_SHIPPING_THRESHOLD, gst

def test_free_shipping_for_500_order():
    product = Product("test_product", 100, 10)
    cart = Cart()
    cart.add(product, 5)  # Corrected the method name to 'add'
    bill = checkout(cart)
    assert bill["shipping"] == money(0), "Expected free shipping for Rs 500 order"
```

## Evidence (pytest output on the buggy version)
```
F
================================== FAILURES ===================================
______________________ test_free_shipping_for_500_order _______________________
test_reproagent_repro.py:10: in test_free_shipping_for_500_order
    assert bill["shipping"] == money(0), "Expected free shipping for Rs 500 order"
E   AssertionError: Expected free shipping for Rs 500 order
E   assert Decimal('50.00') == Decimal('0.00')
E    +  where Decimal('0.00') = money(0)
=========================== short test summary info ===========================
FAILED test_reproagent_repro.py::test_free_shipping_for_500_order - Assertion...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed in 0.09s
```

Analyzer judgement: [evidence-match] actual value 50 is the wrong value the report observed

## Attempt log
- Attempt 1: run=`failed` exception=`AttributeError` -> **invalid_test** — The exception was raised by the test code itself, not by the project: AttributeError: 'Cart' object has no attribute 'add_item'. Check the function signature and how existing tests call it. `Cart` has
- Attempt 2: run=`failed` exception=`NameError` -> **invalid_test** — The test could not run because of a mistake in the test itself: NameError: name 'money' is not defined. Fix imports/names using the module paths and examples shown. `money` is defined in: shopcart.pri
- Attempt 3: run=`failed` exception=`AssertionError` -> **reproduced**
