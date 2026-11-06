# ReproAgent fix report: `shop_discount`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_full`, 2 patch attempt(s), 2 SLM calls, 52.8s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:checkout`, `shopcart/cart.py:Cart.add`, `shopcart/pricing.py:bulk_discount`, `shopcart/pricing.py:money`, `shopcart/pricing.py:gst`, `shopcart/pricing.py:shipping_fee`

Regression tests run: tests/test_cart.py

### Proposed patch
```diff
--- a/shopcart/pricing.py
+++ b/shopcart/pricing.py
@@ -17,8 +17,8 @@
 def bulk_discount(subtotal) -> Decimal:
     """10% discount for orders of Rs 1,000 or more."""
     subtotal = Decimal(str(subtotal))
-    if subtotal >= DISCOUNT_THRESHOLD:
-        return money(subtotal * DISCOUNT_RATE)
+    if subtotal >= Decimal("1000"):  # Corrected threshold to Rs 1,000
+        return money(subtotal * Decimal("0.10"))  # Corrected discount rate to 10%
     return money(0)
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **not_applicable** — Your code is identical to the current bulk_discount, so it changes nothing and the bug is still there. Its body may be correct; the defect may be in what it uses: constants `DISCOUNT_THRESHOLD = 100      # orders of Rs 1
- Patch 2: **plausible**
