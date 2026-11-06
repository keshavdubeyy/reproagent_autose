# ReproAgent fix report: `ml_quantity`

## Bug resolution: **PLAUSIBLE FIX FOUND**  (mode `fix_multi`, 2 patch attempt(s), 2 SLM calls, 46.5s)

**Hidden oracle (evaluation only):** PASS (correct)

Fault localisation (most suspicious first): `shopcart/cart.py:Cart.add`, `shopcart/cart.py:Cart.set_quantity`, `shopcart/cart.py:Cart.__init__`, `shopcart/cart.py:Product.__init__`

Regression tests run: tests/test_cart.py

Changed locations (2): `shopcart/cart.py:Cart.add`, `shopcart/cart.py:Cart.set_quantity`

### Proposed patch
```diff
--- a/shopcart/cart.py
+++ b/shopcart/cart.py
@@ -18,7 +18,7 @@
         self.coupon = None
 
     def add(self, product: Product, quantity: int = 1) -> None:
-        if quantity < 0:
+        if quantity <= 0:
             raise ValueError("quantity must be positive")
         if product.sku in self.items:
             self.items[product.sku][1] += quantity
@@ -27,6 +27,8 @@
 
     def set_quantity(self, sku: str, quantity: int) -> None:
         """Change the quantity of an item; a quantity of 0 removes the item."""
+        if quantity < 0:
+            raise ValueError("quantity must be positive")
         if quantity == 0:
             self.items.pop(sku, None)
             return
```

Validation: the reproduction test now passes and the related existing tests did not regress.

### Patch attempts
- Patch 1: **partial** — Your patch made progress and was KEPT: 1 more reproduction test function(s) now pass, but 1 still fail(s) (test_cart_add_invalid_quantity): Failed: DID NOT RAISE ValueError. The bug needs a change in ANOTHER place too.
- Patch 2: **plausible**
