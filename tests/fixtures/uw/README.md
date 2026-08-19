# UW fixtures

Captured from a live pull on 2026-08-18 against this account's Basic plan
(no `volatility` add-on).

- `svix_quote.json` — real response from `GET /api/stock/SVIX/quote`
- `uvxy_chain.json` — real response from `GET /api/stock/UVXY/option-chains?greeks=true`
- `vix_chain.json` — real response from `GET /api/stock/VIX/option-chains?greeks=true`
  (1520 contracts, 13 expiries out to 2027-04-21). Used by
  `tests/test_unusual_whales.py` to regression-test the put-call-parity
  synthetic VIX/VIX3M fallback.

**No VIX/VIX3M *quote* fixture exists** — `/api/volatility/vix-term-structure`
returns 403 `volatility_scope_required` on this account (needs a separate
paid add-on), `/api/stock/VIX/quote` returns 404 (VIX is an index, not a
"stock" on this API), and `/api/stock/VIX3M/quote` returns 422 (not a
recognized ticker). `data/unusual_whales.py::vix_term()` falls back to
deriving VIX/VIX3M from `vix_chain.json`'s option chain via put-call
parity instead — see that method's docstring for the full methodology and
caveats. Recapture this directory if the account's plan changes.
