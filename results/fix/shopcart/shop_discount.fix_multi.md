# ReproAgent fix report: `shop_discount`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_multi`, 2 patch attempt(s), 3 SLM calls, 70.9s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:checkout`, `shopcart/cart.py:Cart.add`, `shopcart/pricing.py:bulk_discount`, `shopcart/pricing.py:money`, `shopcart/pricing.py:gst`, `shopcart/pricing.py:shipping_fee`

Regression tests run: tests/test_cart.py

Changed locations (1): `shopcart/pricing.py:DISCOUNT_THRESHOLD`

### Proposed patch
```diff
--- a/shopcart/pricing.py
+++ b/shopcart/pricing.py
@@ -1,7 +1,7 @@
 """Pricing rules for the ShopCart demo store (amounts in rupees)."""
 from decimal import ROUND_HALF_UP, Decimal
 
-DISCOUNT_THRESHOLD = 100      # orders of Rs 1,000 or more get a discount
+DISCOUNT_THRESHOLD = 1000      # orders of Rs 1,000 or more get a discount
 DISCOUNT_RATE = Decimal("0.10")
 FREE_SHIPPING_THRESHOLD = 500  # orders of Rs 500 or more ship free
 SHIPPING_FEE = Decimal("50")
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **not_applicable** — Your code is identical to the current bulk_discount, so it changes nothing and the bug is still there. Its body may be correct; the defect may be in what it uses: constants `DISCOUNT_THRESHOLD = 100      # orders of Rs 1
- Patch 2: **plausible**
