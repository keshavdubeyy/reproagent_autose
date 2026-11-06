# ReproAgent fix report: `shop_set_quantity_zero`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_multi`, 1 patch attempt(s), 1 SLM calls, 31.6s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:Cart.set_quantity`, `shopcart/cart.py:Cart.remove`, `shopcart/cart.py:Cart.add`, `shopcart/cart.py:Cart.__init__`, `shopcart/cart.py:Product.__init__`

Regression tests run: tests/test_cart.py

Changed locations (1): `shopcart/cart.py:Cart.set_quantity`

### Proposed patch
```diff
--- a/shopcart/cart.py
+++ b/shopcart/cart.py
@@ -29,7 +29,10 @@
         """Change the quantity of an item; a quantity of 0 removes the item."""
         if quantity < 0:
             raise ValueError("quantity cannot be negative")
-        self.items[sku][1] = quantity
+        if quantity == 0:
+            self.remove(sku)
+        else:
+            self.items[sku][1] = quantity
 
     def remove(self, sku: str) -> bool:
         """Remove an item. Returns False if it was not in the cart."""
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **plausible**
