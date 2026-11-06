# ReproAgent fix report: `shop_coupon`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_multi`, 1 patch attempt(s), 1 SLM calls, 26.9s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:Cart.apply_coupon`, `shopcart/pricing.py:coupon_rate`, `shopcart/cart.py:Cart.__init__`, `shopcart/pricing.py:COUPONS`

Regression tests run: tests/test_cart.py

Changed locations (1): `shopcart/pricing.py:coupon_rate`

### Proposed patch
```diff
--- a/shopcart/pricing.py
+++ b/shopcart/pricing.py
@@ -33,7 +33,8 @@
     """Coupon codes are case-insensitive and ignore surrounding spaces."""
     if not code:
         return Decimal("0")
-    return COUPONS.get(code.strip(), Decimal("0"))
+    # Use strip() to remove leading and trailing spaces and convert to uppercase
+    return COUPONS.get(code.strip().upper(), Decimal("0"))
 
 
 def gst(amount) -> Decimal:
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **plausible**
