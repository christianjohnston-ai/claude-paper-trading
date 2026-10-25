# Recorded responses (real, not synthetic)

These files are verbatim responses captured from public endpoints on 2026-10-24 (UTC) during the
initial build, used to test parsers offline. Directory files are excerpts (header, selected rows,
footer) of the full official files. Do not edit them; re-record instead.

| File | Source URL | Notes |
|---|---|---|
| yahoo_chart_NVDA_2024-06-03_2024-06-12.json | https://query1.finance.yahoo.com/v8/finance/chart/NVDA?period1=1717372800&period2=1718236800&interval=1d&events=div%2Csplits | Contains the 2024-06-10 10:1 split; closes before it are split-adjusted by Yahoo |
| yahoo_chart_SPY_1mo_retrieved_2026-10-24.json | https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1mo&interval=1d&events=div%2Csplits | Last bar 2026-10-23 |
| nasdaqtrader_nasdaqlisted_excerpt_2026-10-23.txt | https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt | Excerpt |
| nasdaqtrader_otherlisted_excerpt_2026-10-23.txt | https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt | Excerpt |
| yahoo_rss_AAPL_retrieved_2026-10-24.xml | https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US | |
| nasdaq_earnings_2026-10-28_retrieved_2026-10-24.json | https://api.nasdaq.com/api/calendar/earnings?date=2026-10-28 | |
