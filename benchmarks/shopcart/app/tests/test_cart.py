"""Existing (incomplete) unit tests of the ShopCart store - none of them catches the seeded bugs."""
from decimal import Decimal

from shopcart import Cart, Product, checkout
from shopcart.pricing import gst


def test_add_and_subtotal():
    cart = Cart()
    cart.add(Product("P1", "Pen", 20), 3)
    assert cart.subtotal() == Decimal("60.00")


def test_checkout_large_order_gets_discount():
    cart = Cart()
    cart.add(Product("TV", "Television", 2000))
    bill = checkout(cart)
    assert bill["discount"] == Decimal("200.00")


def test_gst_simple():
    assert gst(100) == Decimal("18.00")
