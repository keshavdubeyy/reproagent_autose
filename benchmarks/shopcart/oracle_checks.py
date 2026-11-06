"""Hidden oracle checks for ShopCart bugs (evaluation only, never shown to the agent).

Each check fails on the buggy tree and passes on the fixed tree. They test the intended
behaviour more broadly than one example (boundary values), so a patch that only special-cases
the reproduction test's input is scored as plausible but not correct.
"""
ORACLES = {
    "shop_discount": "from shopcart.pricing import bulk_discount\n"
                     "assert bulk_discount(800) == 0 and bulk_discount(999.99) == 0 and bulk_discount(100) == 0\n"
                     "assert bulk_discount(1000) == 100 and bulk_discount(2500) == 250",
    "shop_shipping": "from shopcart.pricing import shipping_fee\n"
                     "assert shipping_fee(500) == 0 and shipping_fee(750) == 0\n"
                     "assert shipping_fee(499.99) == 50 and shipping_fee(10) == 50",
    "shop_coupon": "from decimal import Decimal\nfrom shopcart.pricing import coupon_rate\n"
                   "assert coupon_rate('save10') == Decimal('0.10') and coupon_rate(' Festive20 ') == Decimal('0.20')\n"
                   "assert coupon_rate('SAVE10') == Decimal('0.10') and coupon_rate('nope') == 0",
    "shop_gst": "from decimal import Decimal\nfrom shopcart.pricing import gst\n"
                "assert gst(12.25) == Decimal('2.21') and gst(100) == Decimal('18.00')\n"
                "assert gst(10.03) == Decimal('1.81') and gst(0) == 0",
    "shop_empty_checkout": "from shopcart import Cart, Product, checkout\n"
                           "try:\n    checkout(Cart())\nexcept ValueError as e:\n    assert 'empty' in str(e)\n"
                           "else:\n    raise AssertionError('no ValueError')\n"
                           "c = Cart(); c.add(Product('A', 'a', 100)); assert checkout(c)['subtotal'] == 100",
    "shop_set_quantity_zero": "from shopcart import Cart, Product\n"
                              "c = Cart(); c.add(Product('A', 'a', 10), 2); c.add(Product('B', 'b', 5))\n"
                              "c.set_quantity('A', 0); assert 'A' not in c.items and c.item_count() == 1\n"
                              "c.set_quantity('B', 4); assert c.items['B'][1] == 4",
    "shop_remove_missing": "from shopcart import Cart, Product\n"
                           "c = Cart(); c.add(Product('A', 'a', 10))\n"
                           "assert c.remove('A') is True and c.remove('A') is False and c.remove('Z') is False",
    "shop_stock": "from shopcart import Cart, Product, checkout\n"
                  "p = Product('A', 'a', 10, stock=10); q = Product('B', 'b', 5, stock=4)\n"
                  "c = Cart(); c.add(p, 3); c.add(q, 4); checkout(c)\nassert p.stock == 7 and q.stock == 0",
}
