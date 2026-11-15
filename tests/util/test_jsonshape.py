"""Unit and property tests for :mod:`academic_tools_mcp.util.jsonshape`."""

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp.util import jsonshape

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(),
)
json_values: st.SearchStrategy[Any] = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=10), children, max_size=5),
    ),
    max_leaves=15,
)


class TestJsonShape:
    def test_as_dict(self) -> None:
        assert jsonshape.as_dict({"a": 1}) == {"a": 1}
        assert jsonshape.as_dict(None) == {}
        assert jsonshape.as_dict([1, 2]) == {}
        assert jsonshape.as_dict("str") == {}

    def test_as_int_rejects_bool(self) -> None:
        assert jsonshape.as_int(0) == 0
        assert jsonshape.as_int(-5) == -5
        assert jsonshape.as_int(True) is None
        assert jsonshape.as_int(False) is None
        assert jsonshape.as_int(1.5) is None
        assert jsonshape.as_int("1") is None

    def test_non_neg_int(self) -> None:
        assert jsonshape.non_neg_int(0) == 0
        assert jsonshape.non_neg_int(42) == 42
        assert jsonshape.non_neg_int(-1) is None
        assert jsonshape.non_neg_int(True) is None
        assert jsonshape.non_neg_int(None) is None

    def test_as_bool(self) -> None:
        assert jsonshape.as_bool(True) is True
        assert jsonshape.as_bool(False) is False
        assert jsonshape.as_bool(1) is None
        assert jsonshape.as_bool(0) is None
        assert jsonshape.as_bool("true") is None

    def test_as_str_and_non_empty_str_and_stripped_str(self) -> None:
        assert jsonshape.as_str("abc") == "abc"
        assert jsonshape.as_str("") == ""
        assert jsonshape.as_str(None) == ""
        assert jsonshape.str_or_none("abc") == "abc"
        assert jsonshape.str_or_none("") == ""
        assert jsonshape.str_or_none(None) is None
        assert jsonshape.non_empty_str("abc") == "abc"
        assert jsonshape.non_empty_str("") is None
        assert jsonshape.non_empty_str(None) is None
        assert jsonshape.stripped_str("  hi  ") == "hi"
        assert jsonshape.stripped_str("   ") is None
        assert jsonshape.stripped_str(123) is None

    def test_dict_list_and_str_list_and_unwrap_first(self) -> None:
        assert jsonshape.dict_list([{"a": 1}, "skip", None, {"b": 2}]) == [{"a": 1}, {"b": 2}]
        assert jsonshape.dict_list({"a": 1}) == []
        assert jsonshape.str_list(["a", "", None, "b"]) == ["a", "b"]
        assert jsonshape.str_list("abc") == []
        assert jsonshape.unwrap_first(["title"]) == "title"
        assert jsonshape.unwrap_first([]) is None
        assert jsonshape.unwrap_first("scalar") == "scalar"

    @given(json_values)
    def test_guards_never_raise_on_arbitrary_json(self, value: Any) -> None:
        d = jsonshape.as_dict(value)
        assert isinstance(d, dict)

        i = jsonshape.as_int(value)
        assert i is None or (isinstance(i, int) and not isinstance(i, bool))

        n = jsonshape.non_neg_int(value)
        assert n is None or (isinstance(n, int) and not isinstance(n, bool) and n >= 0)

        b = jsonshape.as_bool(value)
        assert b is None or isinstance(b, bool)

        s = jsonshape.as_str(value)
        assert isinstance(s, str)

        ne = jsonshape.non_empty_str(value)
        assert ne is None or (isinstance(ne, str) and len(ne) > 0)

        st_s = jsonshape.stripped_str(value)
        assert st_s is None or (isinstance(st_s, str) and st_s == st_s.strip() and len(st_s) > 0)

        dl = jsonshape.dict_list(value)
        assert isinstance(dl, list) and all(isinstance(item, dict) for item in dl)

        sl = jsonshape.str_list(value)
        assert isinstance(sl, list) and all(isinstance(item, str) and item for item in sl)

        _ = jsonshape.unwrap_first(value)
