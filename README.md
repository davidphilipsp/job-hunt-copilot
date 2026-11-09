# Job Hunt Copilot

A personal job-search copilot for paid-media, CRM and marketing-engineering roles. It has three
tools:

- **`jobs`**: a finder. It pulls public job feeds, drops what doesn't fit, assigns a lane, scores the
  rest from 0 to 100, and writes a daily Markdown report. The report suggests which of your proofs to
  lead with, names the gap to address, and drafts an application letter.
- **`sonar`**: a local web page over every job the finder kept, with status tracking
  (saved / applied / interview / offer / hidden).
- **`talk`**: a voice interview coach. It asks a question aloud, records your answer, transcribes it
  locally with faster-whisper, flags low-confidence words and logs the attempt. With an API key it
  also returns written feedback.

It **never applies anywhere and never scrapes LinkedIn**. Sources are public APIs, RSS feeds and the
public ATS job-board APIs (Greenhouse, Lever, Ashby). You apply yourself.

> Built with Claude Code as a pair programmer.

## Architecture

```
sources (Remotive, Himalayas, We Work Remotely, Get on Board, Remote OK, Jobicy,
         Working Nomads, Remotar, company ATS feeds from data/watchlist.json)
   → dedupe → location filter (remote / LatAm / worldwide; residency clauses dropped)
   → hard drops (stale > 14 days, other-language fluency, hybrid outside your area, pay below floor, off-core titles)
   → lane: A paid media · C CRM/lifecycle · AC both · D dev/AI for marketing
   → score = lane 30 + edges ≤25 + pay 20 + freshness 15 + seniority 10 − penalties
   → out/YYYY-MM-DD.md (top N, with "lead with", "gap" and a draft letter for the top 3)
   → data/sonar.json → ./bin/sonar (http://127.0.0.1:8770)
```

| File | Role |
|---|---|
| `jobs/finder.py` | Sources, salary parser (USD/EUR/GBP/BRL, ranges, periods → annual USD), filters, lanes, scoring, report, letters |
| `jobs/watchlist.py` | Company career pages via the public Greenhouse / Lever / Ashby APIs |
| `jobs/settings.py` | Loads `config.yaml`. Every personal value lives there, none in code |
| `jobs/sonar.py`, `jobs/sonar.html` | Local-only HTTP server (binds to 127.0.0.1) and a single-page UI |
| `talk/coach.py` | Question deck (generic + built from today's top job) → record → faster-whisper (int8, CPU) → flags → optional Claude feedback → `practice/` log |
| `config.example.yaml` | Every personal setting, as placeholders |

## Stack

Python 3.11+, `requests`, `PyYAML`; `faster-whisper`, `sounddevice` and `numpy` for the coach;
`anthropic` (optional). macOS `say` for spoken questions (skipped elsewhere or with `--quiet`).

## AI used

- **Claude** via the `anthropic` SDK, optional. It drafts letters (`jobs`) and gives interview feedback
  (`talk`) only when `ANTHROPIC_API_KEY` is set **and** `candidate.profile_file` exists. The model ID
  defaults to `claude-sonnet-5-5`. Override it with `llm.model`, `JOBHUNT_MODEL` or `TALK_CLAUDE_MODEL`.
- **faster-whisper** (`small.en` by default) for local speech-to-text.
- Without a key, the finder writes deterministic template letters and the coach logs the transcript and the flagged words.

## Contracts and invariants

- **No invented facts.** The model gets your profile file as its only source of facts. If no profile
  file is configured, no model call is made.
- **No numeric results in letters.** `letter_ok()` rejects percentages, multipliers, ROAS/uplift
  phrasing, banned clichés, emoji, letters of 300+ words, and any digit not in your `allowed_numbers`
  list. A rejected model letter falls back to the template.
- **Never applies, never scrapes.** Network access is limited to public feeds and ATS APIs.
- **Your data stays local.** Seen jobs, statuses and practice logs live under `data/`, `out/` and
  `practice/`, all git-ignored.

## Tests

```bash
python -m venv .venv && .venv/bin/pip install requests pyyaml pytest
.venv/bin/python -m pytest -q        # 8 offline tests: lanes, hard drops, residency, salary parsing, scoring bounds, letter rules
```

## Run

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml        # replace every <PLACEHOLDER>; write only true proofs
cp data/watchlist.example.json data/watchlist.json   # optional: companies to watch
export ANTHROPIC_API_KEY=...              # optional
export JOBHUNT_USD_PER_BRL=0.18           # optional FX for your home currency
./bin/jobs --top 10                       # → out/YYYY-MM-DD.md ; --dry-run, --debug
./bin/sonar                               # local page; --refresh runs the finder first
./bin/talk                                # interview practice; ./bin/talk --review for the week's flagged words
```

The defaults target a candidate based in Brazil: the home currency is BRL, Portuguese-language
postings are detected and Brazil-only roles are tagged. Lanes, edges, penalties and the off-core
title list are generic rules in `jobs/finder.py`. Edit them to fit your own search.

## Security

- Secrets come from the environment only (`ANTHROPIC_API_KEY`). Nothing is hard-coded.
- No personal data is included. Name, city, pay range, employers, proofs and approved numbers were
  moved out of the code into `config.yaml`, which is git-ignored. `config.example.yaml` holds placeholders only.
- The sonar server binds to `127.0.0.1` only.

## License

All rights reserved unless a license file is added.
