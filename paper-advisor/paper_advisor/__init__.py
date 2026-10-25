"""paper-advisor: a local, PAPER-ONLY trading advisor and simulated ledger.

Nothing in this package can place a real order. There is no brokerage client, no brokerage
credential handling, and the only network code is a GET-only, host-allowlisted fetcher for public
market data (see paper_advisor/data_sources/http.py). tests/test_no_live_broker.py enforces this.
"""

__version__ = "1.0.0"
