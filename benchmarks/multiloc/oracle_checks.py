"""Hidden oracle checks for the multi-location bugs (evaluation only, never shown to the agent).
Each covers ALL locations of the bug, so a patch that fixes only one place is scored as not correct."""
ORACLES = {
    "ml_boundaries": "from shopcart.pricing import bulk_discount, shipping_fee\n"
                     "assert bulk_discount(1000) == 100 and bulk_discount(999.99) == 0 and bulk_discount(2000) == 200\n"
                     "assert shipping_fee(500) == 0 and shipping_fee(499.99) == 50 and shipping_fee(800) == 0",
    "ml_quantity": "from shopcart import Cart, Product\n"
                   "def raises(f):\n    try:\n        f()\n    except ValueError:\n        return True\n    return False\n"
                   "c = Cart(); p = Product('A', 'a', 10)\n"
                   "assert raises(lambda: c.add(p, 0)) and raises(lambda: c.add(p, -1)) and not c.items\n"
                   "c.add(p, 2); assert raises(lambda: c.set_quantity('A', -2)) and c.items['A'][1] == 2\n"
                   "c.set_quantity('A', 5); assert c.items['A'][1] == 5\n"
                   "c.set_quantity('A', 0); assert 'A' not in c.items",
    "ml_shipping": "from decimal import Decimal\nfrom shopcart import Cart, Product, checkout\n"
                   "from shopcart.pricing import shipping_fee\n"
                   "assert shipping_fee(300) == 50 and shipping_fee(10) == 50 and shipping_fee(500) == 0\n"
                   "c = Cart(); c.add(Product('A', 'a', 520)); c.apply_coupon('SAVE10')\n"
                   "assert checkout(c)['shipping'] == 50\n"
                   "c = Cart(); c.add(Product('B', 'b', 300)); assert checkout(c)['shipping'] == 50\n"
                   "c = Cart(); c.add(Product('C', 'c', 700)); assert checkout(c)['shipping'] == 0",
    "ml_coupon": "from decimal import Decimal\nfrom shopcart import Cart, Product, checkout\n"
                 "from shopcart.pricing import coupon_rate\n"
                 "assert coupon_rate(' save10 ') == Decimal('0.10') and coupon_rate('Festive20') == Decimal('0.20')\n"
                 "c = Cart(); assert c.apply_coupon(' save10 ')\n"
                 "c = Cart(); c.add(Product('A', 'a', 2000)); c.apply_coupon('SAVE10')\n"
                 "assert checkout(c)['coupon'] == Decimal('180.00')\n"
                 "c = Cart(); c.add(Product('B', 'b', 500)); c.apply_coupon('FESTIVE20')\n"
                 "assert checkout(c)['coupon'] == Decimal('100.00')",
    "ml_gst": "from decimal import Decimal\nfrom shopcart import Cart, Product, checkout\n"
              "from shopcart.pricing import gst\n"
              "assert gst(100) == Decimal('18.00') and gst(900) == Decimal('162.00')\n"
              "c = Cart(); c.add(Product('A', 'a', 1000)); assert checkout(c)['tax'] == Decimal('162.00')\n"
              "c = Cart(); c.add(Product('B', 'b', 200)); assert checkout(c)['tax'] == Decimal('36.00')",
}
