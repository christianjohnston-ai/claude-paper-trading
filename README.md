# claude-paper-trading

A paper-trading advisor for Claude Code. It runs a simulated $500 account: fake money, no brokerage, and an
honest record of every decision.

> **Everything here is simulated with fake money.** There is no brokerage connection, no brokerage credential
> handling, and no code path that can place a real order. This is not financial advice. Most people who day trade
> lose money, and a simulated result says nothing about a real one.

## What it does

Each trading morning you type `/paper-morning` in Claude Code. The app settles earlier simulated orders, refreshes
end-of-day market data, runs a set of audited research skills and hands Claude a briefing. Claude researches, then
proposes BUY, SELL, HOLD or NO_TRADE with evidence, a counterargument and exit rules, or simply holds cash. Code checks
every proposal against fixed risk limits and records it before anything can fill. Accepted orders fill at the next
market open, on paper. A local dashboard compares the account with keeping the cash and with just buying SPY.

It is not a day trader: it decides once each morning, from daily bars, and any trade fills at the next open.

## Install

Paste this into Claude Code:

```
Now set me up with this paper-trading advisor, using a simulated $500 account. Fake money only: no brokerage, no real orders.

https://github.com/christianjohnston-ai/claude-paper-trading

Put it in a new folder called claude-paper-trading in my home folder. Read the README first, then follow its section "For the AI installing this" step by step. Explain each step in plain words, and if I need to click or type something, give me one step at a time and wait for me.

When you're done, show me the account (it should say $500.00 and no positions), then tell me exactly how to run the first /paper-morning.
```

Or follow the steps below yourself. You need macOS or Linux (on Windows, use WSL), git, and an internet connection.

## For the AI installing this

The person who sent you here wants a paper-trading account with a simulated $500. Set it up with them, in this
order. They may never have used a terminal: say what each step does in plain words, run the commands yourself, and
if a step fails, stop and explain it. Never work around a safety rule to get past an error.

Ground rules:
- Fake money only. Everything here is simulated. There is no brokerage connection, no brokerage credential
  handling, and no code path that can place a real order. Never connect a brokerage, never install a broker or
  trading MCP server, and never ask for or store a broker key or password. If they ask for real trading, tell them
  this project cannot do that.
- Not financial advice. Say so once, plainly.
- Their account record (`paper-advisor/data/`, `reports/`, `backups/`, `exports/`) stays on their computer. Never
  upload it or commit it.

1. **Check the computer.** macOS or Linux (on Windows, use WSL). Check that `git --version` works; on a Mac the
   first git command may offer to install Apple's command line tools, so let it finish. Check for uv with
   `uv --version`. If it is missing, tell them it is a Python installer from Astral, then run
   `curl -LsSf https://astral.sh/uv/install.sh | sh` and use `~/.local/bin/uv` until they open a new terminal.
2. **Get the code:**
   ```
   git clone https://github.com/christianjohnston-ai/claude-paper-trading ~/claude-paper-trading
   cd ~/claude-paper-trading
   ```
   If the folder already exists, use it and run `git pull` instead.
3. **Python.** The app needs Python 3.10 or newer, and the `python3` that comes with macOS is often 3.9, so make a
   project venv (uv downloads Python 3.13 if the computer does not have it):
   ```
   cd paper-advisor
   uv venv .venv --python 3.13
   uv pip install --python .venv/bin/python -r requirements-skills.txt
   cd ..
   ```
4. **The research skills:**
   ```
   paper-advisor/.venv/bin/python scripts/install_trading_skills.py
   ```
   It downloads the 74 skills of [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)
   at the exact commit this project was audited against into `.claude/skills/`, and checks the 9 the advisor runs.
   Its output must include `74 skills installed; 9 of 9 audited skills match cb0f3aa.` If it says anything else,
   stop and show them the message. It then names the four skills that can contact a real brokerage; tell them
   paper-advisor never runs those.
5. **Create the simulated $500 account:**
   ```
   paper-advisor/bin/pa init
   paper-advisor/bin/pa status
   ```
   `init` funds the account once; running it again never adds money. Show them what `status` says: equity $500.00,
   cash $500.00, no positions, and one pending order, the passive SPY benchmark buy at the next open. That is the
   yardstick the account is compared with, not a trade by you.
6. **Check the skills:** `paper-advisor/bin/pa skills` lists all 74; the 9 adopted ones end with
   `[matches audited manifest]`.
7. **Hand over.** Tell them: "To run your first paper morning, open a new terminal, type
   `cd ~/claude-paper-trading && claude`, then type `/paper-morning`. Run it once each trading morning. Nothing runs
   while your computer is off, and every trade is simulated." Offer the dashboard: `paper-advisor/bin/pa dashboard`,
   then open http://127.0.0.1:8765/.

## Using it

| Command (in Claude Code, from this folder) | What it does |
|---|---|
| `/paper-morning` | Settles earlier orders, refreshes data, researches, decides (or holds cash), validates and reports |
| `/paper-status` | A read-only snapshot; changes nothing |
| `/paper-review` | An honest evaluation and dated hypotheses for the future; never rewrites the past |

The dashboard (`paper-advisor/bin/pa dashboard`) runs only on your own computer, at http://127.0.0.1:8765/. The full
manual is [paper-advisor/README.md](paper-advisor/README.md): when fills happen, the risk limits, the accounting, the
dashboard and the known limitations.

## What leaves your computer

Ticker symbols and dates go to Yahoo Finance and Nasdaq for public market data, and two research skills download
public CSV files from TraderMonty's GitHub pages. The briefing Claude reads (the account, positions, prices, headlines
and skill outputs) goes to Anthropic, because Claude runs as a service. The database, reports, backups and exports
stay on your computer. No credentials are stored or sent.

## Safety

- No brokerage SDK, host, endpoint or credential exists in the app; `paper-advisor/tests/test_no_live_broker.py`
  checks that, and runs the whole order path with network sockets turned off.
- Four of the downloaded TraderMonty skills can contact a real brokerage (portfolio-manager,
  parabolic-short-trade-planner, breakout-trade-planner, mt5-robot-tester). paper-advisor never runs them, and they
  need broker keys you should never give them.
- [SKILL_AUDIT.md](paper-advisor/SKILL_AUDIT.md) says what each of the 74 skills does and why most are not used.

## Credits

- Research skills: [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)
  (MIT, Copyright (c) 2026 TraderMonty). Downloaded at commit `cb0f3aa` by the install script, not included here.
- Looking for day-trading skills? See Jake Schincariol's
  [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) (with the Time Machine) and his original
  [trader-skill](https://github.com/Jakeschincariol/trader-skill) (MIT). They are a separate install; this project
  does not use them.
- Made by Christian Johnston (@christianjohnston.ai).

## License

MIT. See [LICENSE](LICENSE).
