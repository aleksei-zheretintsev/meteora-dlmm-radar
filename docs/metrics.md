# Metrics reference

**Pool-level / 30m-related** (provider values and availability vary):

| Label | Meaning | Reading caveat |
|---|---|---|
| Market Cap (MC) | Token market capitalization | Not pool TVL; upper header can display initial → current |
| Total TVL | Total liquidity supplied to a DLMM pool | Not all liquidity is in the active price range |
| Active TVL | Liquidity available in/around the active bin range | Can drop to 0 |
| Fees | Recent fee amount for the current Meteora 30-minute context | Not an earnings forecast |
| Fees / A TVL | Fees divided by Active TVL, percent | Unavailable or zero active TVL needs special treatment |
| Volume / A TVL | Volume divided by Active TVL, percent | High turnover is not guaranteed profitability |
| Swaps / Traders | Recent trade count / unique traders | Distinct from holders |
| Holders / Top 10 | Token holder count / concentration of large holders | May differ by vendor and exclusions |
| Total LPs | Pool liquidity-provider count | Low count may indicate fragile depth |
| Bin Step / Fee | Bin spacing / base fee of the specific pool | Same token can have multiple pools |
| Bin Rent (SOL) | Possible bin-array account creation rent estimate | On-chain/account-state dependent; estimate not guaranteed |
| Transfer Fee | Token transfer-fee configuration | Not the same as swap fee |

**GMGN-derived token signals**: GMGN Views, GMGN Total Fees (lifetime/aggregate context), Bundler, Phishing*, DEV, Rug %, and token authority states (where available). Provider labels and definitions should not be interpreted as audited security guarantees.

**InsightX-derived signals**:

| Label | Source / meaning | Critical limitation |
|---|---|---|
| Bubble MAX | Max connected-wallet cluster token percentage from `clusters[].pct` | **InsightX**, not Bubblemaps |
| Bubble OTHER | `total_cluster_pct - Bubble MAX`, clamped at 0 | Only discovered clusters |
| Top Wallet | Largest candidate after filtering recognized pools/DEX/vault/exchange labels from Scanner holders | Not proof of independent ownership; empty response → unavailable |
| Honeypot | Scanner `honeypot.score` (0 = no detected restriction; 100 = high risk) | **Risk** score, not trust or rug probability; 0 does not mean safe |

**Color rules:** configurable heuristic thresholds for selected risk metrics. Colors are visualization aids, not buy/sell signals. **Missing data = n/a**, never assumed zero.

**Social links:** official X profile → community → direct post → token-mint search. Hashtags group Telegram pool cards by token symbol.
