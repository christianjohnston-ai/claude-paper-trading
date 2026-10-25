---
description: PAPER TRADING status - show the simulated account without creating decisions or trades (read-only)
allowed-tools: Bash(paper-advisor/bin/pa:*)
---

# /paper-status: read-only snapshot of the paper-trading experiment

Run exactly:

```
paper-advisor/bin/pa status
```

This opens the database read-only. It never refreshes data, settles orders, creates decisions,
orders, fills or snapshots, and it does not take the experiment lock. Do not run any other
paper-advisor command as part of this request.

Summarize for the user, leading with **PAPER TRADING (simulated)**:
- experiment, starting capital and inception;
- equity, cash, invested value, realized/unrealized P&L, using the latest stored end-of-day closes
  (say they are not live quotes);
- return since inception vs keeping cash (+0.00%) and vs the SPY benchmark, with the sample size;
- open positions and pending orders, including which session open each pending order targets and
  that it will be settled by the next `/paper-morning` after that session closes;
- data freshness, any held lock (a run in progress) and the last runs' outcomes.

If the output says it is not initialized, tell the user to run `/paper-morning` (which initializes
the $500 experiment once) or `paper-advisor/bin/pa init`.
