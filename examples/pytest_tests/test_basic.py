"""Basic example pytest tests for gvtest integration."""


def test_addition():
    assert 1 + 1 == 2


def test_string_upper():
    assert "hello".upper() == "HELLO"


def test_list_append():
    items = [1, 2, 3]
    items.append(4)
    assert items == [1, 2, 3, 4]


def test_dict_lookup():
    data = {"key": "value"}
    assert data["key"] == "value"


class TestMath:
    def test_multiply(self):
        assert 3 * 7 == 21

    def test_divide(self):
        assert 10 / 2 == 5.0

    def test_power(self):
        assert 2 ** 10 == 1024
