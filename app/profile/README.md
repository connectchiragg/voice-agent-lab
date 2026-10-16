# VALR candidate profile and interview self-evaluation

Private data for Chirag's interview-prep briefing calls. Everything under `data/`
is gitignored and written with mode 0600. Compensation figures exist only in
`profile.json`. Every other file masks them as `[redacted]`.

## Run

```
python scripts/build_profile.py --resume "<resume.pdf>"          # data/profile.json
python scripts/import_recordings.py [--from-export DIR]           # v1 analyses and v2 scores
python scripts/score_performance.py [--from-export DIR] --purge   # rescore stored analyses (v2)
python scripts/import_recordings.py --purge                       # delete raw downloads
```

Keys come from `.env`: `FATHOM_API_KEY`, `FIREFLIES_API_KEY` and `ANTHROPIC_API_KEY`.
Models: Claude Haiku 5.5 classifies which meetings are interviews; Claude Sonnet 5.5
does the analysis and scoring.

## Files (paths overridable by env)

| File (env var) | Schema | What the UI reads |
|---|---|---|
| `data/profile.json` (`VALR_PROFILE_PATH`) | 1 | `brief`. The UI must not show `compensation`. |
| `data/evaluation.json` (`VALR_EVALUATION_PATH`) | `schema_version` 2 (absent means 1) | `brief_text` for the prompt; `interviews[]` (v1 analysis); `trends` (v1, 1-5); `performance_path` |
| `data/interview_scores.csv` and `.json` (`VALR_SCORES_PATH`) | 1 | v1 dated 1-5 scores (kept for compatibility) |
| `data/performance.json` (`VALR_PERFORMANCE_PATH`) | 2 | Granular rubric and chart-ready trends (below) |
| `data/performance_scores.csv` (`VALR_PERFORMANCE_CSV`) | 2 | Long table: `date, interview_id, company, company_stage, round_type, outcome, level, dimension, subdimension, score, justification, quote` |

## performance.json (schema 2)

The full key-by-key schema is in the docstring of `app/profile/performance.py`.
The main keys are:

- `rubric`: the dimensions, each with its sub-dimensions:
  - technical: system_design, coding_problem_solving, cs_fundamentals, domain_depth, tradeoff_reasoning
  - communication: conciseness_answer_first, structure, clarity, listening_clarifying, pace_fillers
  - behavioural: star_stories, ownership_impact, motivation_why_us, pushback_curveballs
  - process: time_management, questions_at_end
  - negotiation: anchoring, information_control, competing_offers (scored only when compensation came up)
- `scale`: scores run 1-10. A null score means N/A (the round did not exercise it).
- `interviews[]`: one entry per interview, sorted by date. Each entry has:
  - `overall_score` (holistic) and `overall_computed` (mean of the dimension scores);
  - `dimension_scores`;
  - `scores[dim][sub] = {score, justification, quote}`, where the quote is at most 20 words;
  - `round_type`: screen, technical, system_design, hiring_manager, behavioural, founder or offer;
  - `company` and `company_stage`;
  - `outcome` (advanced, rejected or unknown) with `outcome_evidence`;
  - `do_next_time`: 3 actions;
  - `question_feedback[]`: 3-5 entries of `{question, went_well, improve, model_answer_hint}`.
- `trends.overall`, `trends.dimensions[dim]` and `trends.subdimensions["dim.sub"]` are each a SERIES:
  - `points[]` holds `{x: "YYYY-MM-DD", y, ma3, interview_id, company, round_type}`, so x is the date, y the score and ma3 the 3-interview moving average.
  - Each series also has `n`, `slope_per_30d`, `fitted_change` and `badge`.
- `badge_rule`: the badge is `improving` when the fitted change over the window is at least +1.0 points, `declining` at -1.0 or below, and `flat` otherwise. It is `insufficient_data` with fewer than 3 points.
- `by_round_type[round]` and `by_company_stage[stage]` each hold `{n, overall, dimensions}` (means).
- `callouts.most_improved` and `callouts.biggest_risk` name a sub-dimension `key`; the risk is the lowest latest moving average.
- `recurring_weak_topics[]` holds `{topic, verdict, occurrences[{date, interview_id, status, note}]}`.
- `trend_text`: at most 150 tokens. The same text is the `Trend:` section of `evaluation.json`'s `brief_text`.

## Briefing

`app.profile.briefing.prompt_block()` returns the profile brief followed by
`brief_text` (the evaluation brief, at most 600 tokens). Call it once when a call
starts, outside the realtime audio path, and never log it.
