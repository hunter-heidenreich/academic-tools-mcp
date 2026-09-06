"""Property-based tests for the on-disk cache's round-trip and keying rules.

Three invariants that every provider leans on, held over generated payloads
rather than the handful of shapes anyone thought to write down:

1. **Positive round-trip.** Whatever JSON object a provider hands ``put``,
   ``get`` hands back — nesting, non-ASCII, empty containers and all. This is
   what lets a tool slice a cached response exactly as it would slice a fresh
   one, and it is the reason writes go through ``json.dumps(ensure_ascii=False)``
   and reads name UTF-8 explicitly.
2. **Negative round-trip.** ``get_negative`` returns the payload with only the
   ``_expires_at`` slot removed, so the agent sees the same ``{error: ...}``
   shape it would get from a fresh 404. Caller keys that happen to start with
   ``_`` must survive.
3. **Keying.** ``cache_dir`` plus ``_cache_key`` is a function of the
   (namespace, entity, identifier) triple alone: stable across calls, and
   injective, so two identifiers can never read each other's entry.

The autouse cache-root fixture is function-scoped, so ``max_examples`` runs
share one ``tmp_path``; each example uses its own identifier and the writes are
idempotent anyway.
"""

import string
import unicodedata

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import cache

# Cache payloads are decoded provider JSON: dicts at the top level (put()'s
# signature), with arbitrary JSON below. NaN/Infinity are excluded — json.dumps
# emits them but they are not JSON, so a foreign reader would choke on a file
# we wrote.
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4)
    ),
    max_leaves=12,
)
# Biased towards the keys providers really emit, because that is where a lossy
# read would show: plain `st.text()` essentially never draws an underscore-
# prefixed key, so an over-eager "strip our bookkeeping" change would slip past
# an unbiased strategy.
payload_keys = st.text() | st.sampled_from(
    ["error", "suggestion", "not_found", "_canonical_id", "_source", "_expires_at"]
)
json_objects = st.dictionaries(payload_keys, json_values, max_size=6)

# Identifiers are DOIs, arXiv IDs, URLs and titles — anything an agent can
# type. They are hashed before they reach the filesystem, so the strategy is
# deliberately hostile: path separators, dots and non-ASCII included.
identifiers = st.text(min_size=1, max_size=60)

# Namespaces and entities are module-level literals in the providers, never
# user input; keep the strategy to what those actually look like.
names = st.text(alphabet=string.ascii_lowercase + "_", min_size=1, max_size=12)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(identifiers, json_objects)
def test_put_then_get_round_trips_any_payload(identifier: str, payload: dict) -> None:
    assert cache.put("openalex", "works", identifier, payload) is True
    assert cache.get("openalex", "works", identifier) == payload


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(identifiers, json_objects)
def test_put_negative_round_trips_everything_but_its_own_key(
    identifier: str, payload: dict
) -> None:
    """Only ``_expires_at`` is stripped. Every other key — including the
    underscore-prefixed ones providers really use, like ``_canonical_id`` —
    comes back untouched."""
    expected = {k: v for k, v in payload.items() if k != "_expires_at"}

    assert cache.put_negative("arxiv", "papers", identifier, payload, ttl_seconds=600.0) is True
    assert cache.get_negative("arxiv", "papers", identifier) == expected


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(identifiers, json_objects, json_objects)
def test_positive_and_negative_halves_never_shadow_each_other(
    identifier: str, positive: dict, negative: dict
) -> None:
    """The ``_neg/`` sibling directory means one identifier can hold both a
    positive and a negative entry; neither read may see the other's file, so a
    DOI that starts resolving is served even while its 404 is still cached."""
    cache.put("openalex", "works", identifier, positive)
    cache.put_negative("openalex", "works", identifier, negative, ttl_seconds=600.0)

    assert cache.get("openalex", "works", identifier) == positive
    assert cache.get_negative("openalex", "works", identifier) == {
        k: v for k, v in negative.items() if k != "_expires_at"
    }

    # And invalidate takes both halves, which is what force_refresh relies on.
    cache.invalidate("openalex", "works", identifier)
    assert cache.get("openalex", "works", identifier) is None
    assert cache.get_negative("openalex", "works", identifier) is None


@given(names, names, identifiers)
def test_cache_key_is_deterministic(namespace: str, entity: str, identifier: str) -> None:
    """Same triple, same path — every time. A key that drifted (dict ordering,
    locale, a stray strip) would silently turn every cache read into a miss."""
    first = cache.cache_dir(namespace, entity) / cache._cache_key(identifier)
    second = cache.cache_dir(namespace, entity) / cache._cache_key(identifier)
    assert first == second


# Unrelated identifiers almost never collide by accident, so the interesting
# regime is *near-misses*: pairs a lossy key function would fold together. Case
# folding, whitespace trimming and Unicode normalization are the plausible
# slips, and each is a real distinction upstream — arXiv IDs are case-sensitive,
# and `_doi.canonical` is where deliberate folding belongs, not here.
_VARIANTS = (
    str.swapcase,
    str.lower,
    str.upper,
    str.strip,
    lambda s: s + " ",
    lambda s: s.replace("/", "_"),
    lambda s: unicodedata.normalize("NFD", s),
    lambda s: unicodedata.normalize("NFKC", s),
)
identifier_pairs = st.tuples(identifiers, identifiers) | st.builds(
    lambda s, f: (s, f(s)), identifiers, st.sampled_from(_VARIANTS)
)


@given(identifier_pairs)
def test_distinct_identifiers_never_share_a_key(pair: tuple[str, str]) -> None:
    """Injective within one (namespace, entity): the hash is what makes an
    arbitrary DOI, URL or title safe as a filename, and two papers sharing a
    file would serve one's metadata for the other."""
    a, b = pair
    assert (cache._cache_key(a) == cache._cache_key(b)) == (a == b)


@given(names, names, names, names, identifiers)
def test_namespace_and_entity_partition_the_keyspace(
    ns_a: str, ns_b: str, ent_a: str, ent_b: str, identifier: str
) -> None:
    """One identifier under two providers (an arXiv paper that is also an
    OpenAlex work) resolves to two independent entries."""
    path_a = cache.cache_dir(ns_a, ent_a) / cache._cache_key(identifier)
    path_b = cache.cache_dir(ns_b, ent_b) / cache._cache_key(identifier)
    assert (path_a == path_b) == ((ns_a, ent_a) == (ns_b, ent_b))
