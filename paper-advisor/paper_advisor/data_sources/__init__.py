"""External data adapters. All network access in the application goes through http.Fetcher, which is
GET-only and restricted to an allowlist of public market-data hosts. No brokerage host is (or may
be) on the allowlist; tests/test_no_live_broker.py enforces that."""
