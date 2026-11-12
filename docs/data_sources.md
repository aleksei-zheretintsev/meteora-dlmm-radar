# Sources, access and limitations

| Source | Function in the bot | Limitations |
|---|---|---|
| Meteora DLMM Data / Discovery API | Pool discovery, detail, 30m metrics, fees, liquidity, LP count | Feed coverage, pagination, latency and missing values; API format can change |
| GMGN | Token metadata and available token/security indicators | Some endpoints are browser-observed rather than documented developer APIs; may break |
| InsightX Clusters | Related-wallet cluster concentration | Not identical to Bubblemaps topology; free-tier quotas apply |
| InsightX Scanner + Labels | Honeypot score, top holders and infrastructure labels | HTTP 200 can have empty holders; incomplete labels; quota/caching |
| DEX Screener | Public token social links when available | Does not guarantee verified links or follower counts |
| Solana RPC | On-chain configuration / technical account checks | Endpoint reliability and request cost vary |
| Telegram Bot API | Photo delivery, interactive buttons and message edits | Requires bot token, one active `getUpdates` consumer |
| Bubblemaps | External research link | Its map is not the source of Bubble MAX/OTHER numbers |

Do not describe the bot as having direct X follower/member data access: it was intentionally excluded after API/availability evaluation. Neither a live trading API nor autonomous order execution is included.

Data captured at different times or by different vendors may differ without implying that one provider is universally correct. API limits and vendor access terms should be rechecked before public deployment.
