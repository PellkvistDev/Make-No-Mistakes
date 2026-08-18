from cart import total


def test_single_item():
    assert total([{"name": "pen", "price": 10.0, "qty": 1}]) == 12.0


def test_several_items():
    assert total([{"name": "pen", "price": 10.0, "qty": 2},
                  {"name": "pad", "price": 5.0, "qty": 1}]) == 30.0


def test_empty_cart():
    assert total([]) == 0.0
