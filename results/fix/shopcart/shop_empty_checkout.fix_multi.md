# ReproAgent fix report: `shop_empty_checkout`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_multi`, 1 patch attempt(s), 1 SLM calls, 49.5s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:checkout`, `shopcart/pricing.py:bulk_discount`, `shopcart/pricing.py:money`, `shopcart/pricing.py:gst`, `shopcart/pricing.py:shipping_fee`, `shopcart/pricing.py:coupon_rate`

Regression tests run: tests/test_cart.py

Changed locations (1): `shopcart/cart.py:checkout`

### Proposed patch
```diff
--- a/shopcart/cart.py
+++ b/shopcart/cart.py
@@ -2,6 +2,7 @@
 from decimal import Decimal
 
 from .pricing import bulk_discount, coupon_rate, gst, money, shipping_fee
+from decimal import Decimal, ROUND_HALF_UP
 
 
 class Product:
@@ -53,6 +54,9 @@
 
 def checkout(cart: Cart) -> dict:
     """Compute the final bill. Raises ValueError for an empty cart."""
+    if cart.item_count() == 0:
+        raise ValueError("cart is empty")
+
     subtotal = cart.subtotal()
     discount = bulk_discount(subtotal)
     coupon = money((subtotal - discount) * coupon_rate(cart.coupon))
@@ -67,5 +71,5 @@
     return {
         "subtotal": subtotal, "discount": discount, "coupon": coupon, "tax": tax,
         "shipping": ship, "total": money(taxable + tax + ship),
-        "average_item_price": money(subtotal / cart.item_count()),
+        "average_item_price": money(subtotal / cart.item_count() if cart.item_count() > 0 else 0),
     }
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **plausible**
