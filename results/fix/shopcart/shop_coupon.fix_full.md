# ReproAgent fix report: `shop_coupon`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_full`, 1 patch attempt(s), 1 SLM calls, 26.5s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:Cart.apply_coupon`, `shopcart/pricing.py:coupon_rate`, `shopcart/cart.py:Cart.__init__`, `shopcart/pricing.py:COUPONS`

Regression tests run: tests/test_cart.py

### Proposed patch
```diff
--- a/shopcart/pricing.py
+++ b/shopcart/pricing.py
@@ -1,5 +1,6 @@
 """Pricing rules for the ShopCart demo store (amounts in rupees)."""
 from decimal import ROUND_HALF_UP, Decimal
+from decimal import Decimal
 
 DISCOUNT_THRESHOLD = 1000      # orders of Rs 1,000 or more get a discount
 DISCOUNT_RATE = Decimal("0.10")
@@ -33,7 +34,9 @@
     """Coupon codes are case-insensitive and ignore surrounding spaces."""
     if not code:
         return Decimal("0")
-    return COUPONS.get(code.strip(), Decimal("0"))
+    # Convert to uppercase and strip spaces
+    code = code.upper().strip()
+    return COUPONS.get(code, Decimal("0"))
 
 
 def gst(amount) -> Decimal:
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **plausible**
