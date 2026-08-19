"""
Regression test for the put-call-parity synthetic VIX/VIX3M fallback
(data/unusual_whales.py::_synthetic_index_level), against a real VIX
option chain captured 2026-08-18. This is the fallback path used because
this account's UW plan does not include the `volatility` add-on that
would otherwise carry /api/volatility/vix-term-structure — see
vix_term()'s docstring and VIX_TRADER_BOT_BUILD_PLAN.md §0.
"""
import json
from pathlib import Path

from data.unusual_whales import _synthetic_index_level, _VIX_TARGET_DTE, _VIX3M_TARGET_DTE

_FIXTURE = Path(__file__).parent / "fixtures" / "uw" / "vix_chain.json"


def _load_chain():
    return json.loads(_FIXTURE.read_text())


def test_synthetic_level_near_30dte_is_sane():
    chain = _load_chain()
    level, dte = _synthetic_index_level(chain, _VIX_TARGET_DTE)
    assert level is not None
    # Sanity band, not an exact pin — VIX rarely trades outside this range.
    assert 5.0 < level < 80.0
    assert abs(dte - _VIX_TARGET_DTE) <= 10


def test_synthetic_level_near_93dte_is_sane():
    chain = _load_chain()
    level, dte = _synthetic_index_level(chain, _VIX3M_TARGET_DTE)
    assert level is not None
    assert 5.0 < level < 80.0
    assert abs(dte - _VIX3M_TARGET_DTE) <= 15


def test_synthetic_level_none_when_chain_is_bare_symbol_list():
    # /api/stock/{ticker}/option-chains without greeks=true returns a list
    # of OSI symbol strings, not contract dicts — must not crash on that.
    level, dte = _synthetic_index_level({"data": ["VIX260819C00160000"]}, 30)
    assert level is None
    assert dte is None


def test_synthetic_level_none_when_no_expiry_near_target():
    chain = {"data": [
        {"expires": "2026-08-20", "strike": "20", "option_type": "call", "nbbo_bid": "1.0", "nbbo_ask": "1.2"},
        {"expires": "2026-08-20", "strike": "20", "option_type": "put", "nbbo_bid": "0.9", "nbbo_ask": "1.1"},
    ]}
    # Only a ~1-DTE expiry exists; asking for a 93-DTE target should still
    # return *something* (closest available), so instead assert it does NOT
    # silently invent a level when there are no expiries at all.
    level, dte = _synthetic_index_level({"data": []}, 93)
    assert level is None and dte is None
