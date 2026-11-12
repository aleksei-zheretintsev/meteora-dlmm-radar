# Testing, verification and known issues

## Test categories

- **Static:** `python -m py_compile radar.py`.
- **Social routing:** run `python radar.py --test-social-routing` (four synthetic cases: profile, community, post, fallback search).
- **Live social check:** `python radar.py --check-social-mint <TOKEN_MINT>` (verified on a woohao community in development; availability may change).
- **InsightX:** `--check-bubble-mint <TOKEN_MINT>` and `--check-risk-mint <TOKEN_MINT>` for source response and holder-label inspection.
- **Telegram:** confirm new message, X links, Update, Update 30MIN, initial snapshot preservation and token hashtag search.
- **Operational:** launch from Windows shortcut; Ctrl+C to stop; second process should be rejected.

## Observed development checks

- Four social-routing local tests passed in the V46 development run; real community URL lookup returned a community URL for woohao.
- InsightX risk and cluster requests returned HTTP 200 on tested tokens. Some tokens returned an empty `top_holders` list despite 200.
- Recognized PumpSwap liquidity account was excluded from Top Wallet in a specific woohao audit, but an unlabeled candidate remained unverified.

These are **observations**, not a production SLO, full test-suite coverage, or a guarantee of results across all tokens.

## Open verification items

1. Verify live X post deep links from incoming token metadata; synthetic routing tests alone are insufficient.
2. Audit Top Wallet on additional real tokens against owner addresses in GMGN or on-chain records; avoid treating unlabeled accounts as independently owned.
3. Check 30-minute provider consistency and refresh behavior under missing/zero-liquidity conditions.
4. Measure InsightX quota usage and API failures during sustained monitoring.
5. Verify public package contains no credentials, private files or sample real keys.

**Security:** never attach `.env`, `.pem`, token text files, production databases, or full debug responses containing secrets to public issues or the repository.
