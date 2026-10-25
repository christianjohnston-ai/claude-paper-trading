---
description: PAPER TRADING review - evaluate the simulated results honestly and propose dated, prospective strategy hypotheses
allowed-tools: Bash(paper-advisor/bin/pa:*), Read, Write
---

# /paper-review: honest evaluation and prospective hypotheses

You are reviewing a PAPER-ONLY simulated experiment. A review can never change past records: the
decisions, orders, fills and valuations are append-only. What you produce is an evaluation plus
dated hypotheses that, if a human later adopts them, only affect FUTURE decisions.

## Step 1: build the review briefing

```
paper-advisor/bin/pa review start --model-id <YOUR EXACT MODEL ID> --effort <EFFORT OR unknown>
```

Use the exact model ID from your system context; pass an effort level only if your context states
it, otherwise `unknown`. Read the `briefing_path` JSON it prints.

## Step 2: evaluate (skeptically)

Cover, with numbers from the briefing only:
- portfolio return net of modeled costs vs keeping cash (+0.00%) vs the passive SPY benchmark
  (same start, costs and dividend treatment), max drawdown, exposure, turnover;
- realized vs unrealized P&L; closed-trade win rate, average win/loss and profit factor only with
  their sample size;
- decision outcomes: each filled BUY vs SPY over the same window; NO_TRADE days vs what SPY did;
  rejected proposals and why they were rejected;
- confidence calibration only if a bucket has 20+ decisions (otherwise say it cannot be assessed);
- the verdicts of the audited skills in `skills` exactly as reported (for example
  `residual-edge-analyzer` returning INSUFFICIENT_EVIDENCE below 60 daily observations, and the
  `backtest-expert` gate NOT_EVALUABLE below 30 trades). Early in the experiment, "insufficient
  evidence" is the correct conclusion; do not round it up to "promising".
- Never annualize short samples. Never describe results as skill from a small sample. Remember that
  an LLM's prior knowledge can contaminate any retrospective reasoning about past market moves.

## Step 3: propose at most 3 prospective hypotheses (optional)

Write `paper-advisor/data/runs/<run_id>/review.json`:

```json
{"schema_version": 1, "run_id": <run_id>,
 "model": {"provider": "anthropic/claude-code", "model_id": "<id>", "effort": "<effort or unknown>"},
 "observations": "<= 3000 chars: the evaluation above, concise",
 "hypotheses": [{"title": "...", "hypothesis": "a falsifiable statement about FUTURE decisions",
                 "rationale": "the evidence that motivated it (cite run/decision ids)",
                 "falsification_test": "what result over what sample would prove it wrong",
                 "minimum_sample": "e.g. 30 entries or 60 sessions",
                 "proposed_change": {"type": "none|config|strategy", "config_overrides": {}, "strategy_text": ""},
                 "evidence_refs": []}]}
```

Rules: a hypothesis must be dated and prospective; do not tune rules to fit past trades and present
the tuned version as if it had been followed; `config` changes may only use editable risk keys.

Then submit:

```
paper-advisor/bin/pa review submit --run <run_id> --file paper-advisor/data/runs/<run_id>/review.json
```

## Step 4: report

Lead with **PAPER TRADING (simulated)**. Give the evaluation in plain language, list hypothesis ids
with their earliest effective session, and say that nothing changes until the user explicitly runs
`paper-advisor/bin/pa review adopt --hypothesis <id> --reason "<why>"` (which creates a new
config/strategy version effective from that moment only). Do not adopt hypotheses yourself.
