# Architecture · Meteora DLMM Radar

This document describes **stable component responsibilities** rather than a fixed version number. Technical changes are recorded in `CHANGELOG.md`.

## Data acquisition

1. **Discovery:** Meteora's discovery feed plus a legacy pool-list fallback is used to identify DLMM candidates. A configurable polling interval and discovery pagination determine coverage.
2. **Eligibility:** pool type, creation age, market cap, base fee, and recent fees decide which candidates move to analysis. Missing values can prevent a pool from passing rather than being invented.
3. **Detailed snapshot:** fetch/merge pool details and 30-minute Discovery metrics, preserving source distinctions.
4. **Enrichment:** GMGN supplies available token/security fields; InsightX supplies wallet clusters, the scanner and labels; DEX Screener may supply social URLs; Solana RPC is used for specific technical checks such as bin-array rent and transfer fees.
5. **Analytics:** normalize missing values, render ratios and risk categories, exclude recognizable infrastructure wallets from estimated Top Wallet.
6. **Persistence:** SQLite stores discovery history and original Telegram-card payloads. Separate message metadata lets callbacks locate the right pool.
7. **Delivery:** Pillow renders the two-panel image; Telegram Bot API sends a photo, research links and callback buttons.

## Snapshot and refresh semantics

- The **upper** panel preserves metrics from the original pool discovery card (with market-cap header showing initial → refreshed value when updated).
- The **lower** panel shows refreshed values. Initially, its time-series columns may be blank until the user refreshes the card; token-level risk metrics can be populated earlier.
- **Update:** fetches the current 30-minute Meteora snapshot and computes applicable percentage changes relative to the **initial card**, not necessarily a five-minute candle.
- **Update 30MIN:** also fetches the current 30-minute Meteora snapshot, but uses Meteora's reported 30-minute change fields. Incomplete required 30-minute data prevents the card from being overwritten.
- InsightX wallet and Honeypot values use an approximately **120-second in-process cache** (source code V46); values are not necessarily fetched anew on every button press.
- The card is **edited in place** on successful updates rather than sending a new standalone card.

## Concurrency and fault handling

- A local TCP bind lock rejects a second running instance (avoids Telegram polling conflicts and duplicate cards).
- SQLite stores sent status, original snapshot and callback association. Retry behavior avoids duplicate resend when Telegram delivery succeeded but local persistence failed.
- Best-effort enrichment failures should not silently be reinterpreted as safe or zero-risk results.
- Telegram callback processing uses `getUpdates` with one consumer; background callback loop operates while the scanner runs.

## Data quality caveats

- GMGN browser-observed endpoints may be unofficial and may change without notice.
- InsightX Scanner can return HTTP 200 with an empty holder list. Missing labels do not prove a wallet is independent.
- Clusters are provider-specific; Bubblemaps graphical clusters use different methods.
- A token can have multiple DLMM pools with different fees/bin steps but the same token-level wallet metrics.
- An X link is only direct if source metadata contains an identifiable profile/community/post URL; otherwise the fallback is token search.

## Security boundary

The working folder may contain `.env`, token files, `.pem` keys, SQLite history, logs, and backups. None belong in the public repository. Publish an explicitly reviewed copy, not the full working directory.
