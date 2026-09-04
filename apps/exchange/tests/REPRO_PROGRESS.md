# R-exchange reproduction pass — progress

Scratch bookkeeping for the `repro/R-exchange` lane. **Delete this file once the gates are
recorded.** One line per ticket: ticket id | test node id | both directions confirmed?

"Both directions confirmed" means BOTH of these were run and observed:
- `PROXYSHOP_WORKER=0 .venv/bin/python -m pytest <file> -q -k <name>` -> `xfailed`, exit 0
- `PROXYSHOP_WORKER=0 .venv/bin/python -m pytest <file> -q --runxfail -k <name>` -> `N failed`,
  and the failure is the DEFECT (not a typo, import error or missing fixture)

## Done — both directions confirmed

- T-223 | apps/exchange/tests/test_repro_untrusted_roster.py::test_t223_one_tenth_of_a_cent_does_not_buy_a_hundred_dollar_product[None] | YES
- T-223 | apps/exchange/tests/test_repro_untrusted_roster.py::test_t223_one_tenth_of_a_cent_does_not_buy_a_hundred_dollar_product[100.0] | YES
- T-223 | apps/exchange/tests/test_repro_untrusted_roster.py::test_t223_an_undeclared_billionth_of_a_cent_is_not_a_rankable_bid | YES
- T-224 | apps/exchange/tests/test_repro_untrusted_roster.py::test_t224_an_unreadable_store_price_never_becomes_a_server_error | YES
- T-224 | apps/exchange/tests/test_repro_untrusted_roster.py::test_t224_a_roster_row_that_prices_nothing_cannot_mint_a_free_offer | YES

Gate commands:
- T-223: `pytest apps/exchange/tests/test_repro_untrusted_roster.py -q --runxfail -k t223` -> 3 failed
- T-224: `pytest apps/exchange/tests/test_repro_untrusted_roster.py -q --runxfail -k t224` -> 2 failed

Measured on the tree at 110f23f:
- T-223 cap100 @0.001 declared 100% -> `HTTP 201 entries=[{fallback: false, unit_price: 0.001}]`
- T-223 cap100 @0.001 silent        -> `HTTP 201 entries=[{fallback: false, unit_price: 0.001}]`
- T-223 uncapped(list 100) @1e-09   -> `HTTP 201 entries=[{fallback: false, unit_price: 1e-09}]`
- T-224 list_price 0.0 + "cheap"    -> `HTTP 500  ValueError("could not convert string to float: 'cheap'")`
- T-224 list_price 0.0 + silent     -> `HTTP 201 entries=[{fallback: true, unit_price: 0.0, fallback_reason: 'no_response'}]`
- T-224 list_price 0.0 + -5.0       -> `HTTP 201 entries=[{fallback: false, unit_price: -5.0}]`  (extra, not yet pinned)
- T-224 list_price 0.0 + nan        -> `HTTP 201 entries=[{fallback: false, unit_price: null}]`  (extra, not yet pinned)

## Suite baseline

`PROXYSHOP_WORKER=0 ./scripts/verify.sh pytest` at 110f23f:
`4707 passed, 265 skipped, 1 deselected, 5 xfailed in 124.65s` — **green**.

## Remaining tickets in this lane (not yet written)

T-145 T-146 T-147 T-148 T-150 T-157 T-158 T-159 T-161 T-162 T-168 T-169 T-170 T-177
T-182 T-184 T-185 T-190 T-194 T-195 T-202 T-204 T-215 T-222 T-235 T-240

Reconnaissance is in flight for all of them (five read-only agents, results not yet in).
No test file has been written for any of these yet.
