"""Import -> classify -> evaluate -> save. The CLI is scripts/import_recordings.py.

Long-term storage is evaluation.json (derived analysis only). Normalised
meetings with transcripts exist only in memory and, as raw vendor pages, in the
private cache directory until `--purge`.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.profile import classify, evaluate, impressions, performance, recordings, rubric, trend
from app.profile.config import Settings, write_private
from app.profile.llm import JsonLLM, LLMError
from app.profile.profile import redact_compensation

log = logging.getLogger(__name__)


@dataclass
class Sources:
    fathom_key: str | None = None
    fireflies_key: str | None = None
    export_path: Path | None = None
    http: recordings.Http = field(default_factory=recordings.Http)


def fetch_all(sources: Sources, since: datetime, cache: recordings.RawCache | None
              ) -> tuple[list[dict], dict]:
    meetings, status = [], {}
    if sources.export_path:
        exported = [m for m in recordings.load_export(sources.export_path)
                    if recordings.in_window(m, since)]
        meetings += exported
        status["export"] = f"{len(exported)} meetings"
    for name, key, fetch, normalize, interval in (
            ("fathom", sources.fathom_key, recordings.fetch_fathom,
             recordings.normalize_fathom, 2.0),  # heavy requests: 30/min
            ("fireflies", sources.fireflies_key, recordings.fetch_fireflies,
             recordings.normalize_fireflies, 2.1)):  # free plan: 30/min, 500/day
        if not key:
            status[name] = "skipped (no API key)"
            continue
        sources.http.min_interval = max(sources.http.min_interval, interval)
        try:
            kwargs = ({"want_sentences": lambda m: classify.heuristic(m) != "unlikely"}
                      if name == "fireflies" else {})
            raw = fetch(key, since, sources.http, cache, **kwargs)
        except recordings.ApiError as exc:
            status[name] = f"failed: {exc}"
            continue
        got = [normalize(item) for item in raw]
        meetings += got
        status[name] = f"{len(got)} meetings"
    return meetings, status


def run(cfg: Settings, sources: Sources, llm: JsonLLM | None, days: int = 60,
        cache: recordings.RawCache | None = None, comp: dict | None = None,
        now: datetime | None = None) -> dict:
    since = recordings.window_start(days, now)
    meetings, status = fetch_all(sources, since, cache)
    labels = [(m, classify.classify(m, llm, cfg.classifier_model, cfg.candidate_name))
              for m in meetings]
    interviews = [(m, label) for m, label in labels if classify.is_owner_interview(label)]
    log.info("%d meetings, %d interviews where %s is the candidate", len(meetings),
             len(interviews), cfg.candidate_name)

    analyses, errors = [], []
    if llm is not None:
        for meeting, label in interviews:
            if not meeting["turns"]:
                errors.append(f"{meeting['source']}:{meeting['id']} has no transcript")
                continue
            try:
                analyses.append(evaluate.analyze_interview(
                    meeting, label, llm, cfg.evaluator_model, cfg.candidate_name))
            except LLMError as exc:
                errors.append(f"{meeting['source']}:{meeting['id']}: {exc}")
    result = evaluate.aggregate(analyses, llm, cfg.evaluator_model, cfg.candidate_name) \
        if llm is not None else {"brief": None, "text": "LLM disabled; no evaluation."}

    stats = {"window_days": days, "sources": status, "meetings": len(meetings),
             "classified_by_llm": sum(1 for _, l in labels if l["stage"] == "llm"),
             "interviews": len(interviews), "analysed": len(analyses), "errors": errors,
             "interview_titles": [m["title"] for m, _ in interviews]}
    record = evaluate.evaluation_record(analyses, result, stats, {})
    if llm is not None:
        rows = result.get("score_rows") or []
        if rows:
            write_private(cfg.scores_path, redact_compensation(trend.to_csv(rows), comp))
            write_private(cfg.scores_path.with_suffix(".json"), redact_compensation(
                trend.table_json(rows, result["trends"]), comp) + "\n")
        by_id = {f"{m['source']}:{m['id']}": m for m, _ in interviews}
        record = score_performance(cfg, record, by_id, llm, comp)
    # The owner's own comp figures belong only in profile.json.
    text = redact_compensation(json.dumps(record, indent=2, ensure_ascii=False), comp)
    if llm is not None:
        write_private(cfg.evaluation_path, text + "\n")
    return json.loads(text)


def score_performance(cfg: Settings, record: dict, meetings_by_id: dict, llm: JsonLLM,
                      comp: dict | None) -> dict:
    """Schema v2: granular rubric per interview -> performance.json + CSV; the evaluation
    record gains schema_version 3, a plain-language Trend section and a pointer to performance.json.
    All v1 keys are kept, so v1 readers keep working."""
    analyses = record.get("interviews") or []
    scored, errors = [], []
    # Resume support: finished interviews are kept (0600) until the whole run succeeds.
    partial_path = cfg.performance_path.with_suffix(".partial.json")
    try:
        done = json.loads(partial_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    for n, a in enumerate(analyses, 1):
        iid = f"{a.get('source')}:{a.get('id')}"
        if iid in done:
            scored.append(done[iid])
            continue
        log.info("scoring %d/%d", n, len(analyses))
        try:
            item = rubric.score_interview(meetings_by_id.get(iid), a, llm,
                                          cfg.evaluator_model, cfg.candidate_name)
        except LLMError as exc:
            errors.append(f"{iid}: {exc}")
            continue
        scored.append(item)
        done[iid] = item
        write_private(partial_path, redact_compensation(json.dumps(done), comp))
    topics = (performance.recurring_weak_topics(analyses, scored, llm, cfg.evaluator_model,
                                                cfg.candidate_name) if scored else [])
    if analyses and not scored:
        raise LLMError(f"no interview could be scored ({errors[0] if errors else 'unknown'})")
    previous = read_json(cfg.performance_path)
    errors += attach_impressions(scored, meetings_by_id, {f"{a.get('source')}:{a.get('id')}": a for a in analyses},
                       previous, llm, cfg)
    perf = performance.build(scored, topics)
    perf = performance.with_coach(perf, llm, cfg.evaluator_model, cfg.candidate_name, comp,
                                  previous=(previous or {}).get("coach"))
    perf["errors"] = errors
    if not errors:
        partial_path.unlink(missing_ok=True)
    write_private(cfg.performance_path, redact_compensation(
        json.dumps(perf, indent=2, ensure_ascii=False), comp) + "\n")
    write_private(cfg.performance_csv, redact_compensation(performance.to_csv(perf), comp))
    record = dict(record)
    record["schema_version"] = rubric.SCHEMA_VERSION
    record["performance_path"] = str(cfg.performance_path.name)
    if record.get("brief"):
        record["brief"] = dict(record["brief"], trend=perf["trend_text"])
        record["brief_text"] = evaluate.fit_brief(record["brief"], len(analyses))
    record["token_spend"] = llm.ledger.calls
    log.info("performance v2: %d interviews scored, %d errors", len(scored), len(errors))
    return record


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def attach_impressions(scored: list[dict], meetings_by_id: dict, analyses_by_id: dict,
                       previous: dict | None, llm: JsonLLM | None, cfg: Settings) -> list[str]:
    """Set `impression` on each scored interview (in place): from its transcript when this
    run has one (reused when the transcript is unchanged), else the stored analysis's talk
    balance; an earlier transcript-based impression is kept over a derived one."""
    before = {iv.get("interview_id"): iv.get("impression")
              for iv in (previous or {}).get("interviews") or [] if isinstance(iv, dict)}
    errors = []
    for item in scored:
        iid = item["interview_id"]
        old = before.get(iid) or item.get("impression")
        meeting = meetings_by_id.get(iid)
        if meeting and meeting.get("turns"):
            if old and old.get("source_hash") == impressions.transcript_hash(meeting["turns"]) \
                    and old.get("coverage") == "transcript":
                new = old
            else:
                try:
                    new = impressions.analyze(meeting, llm, cfg.evaluator_model, cfg.candidate_name)
                except LLMError as exc:
                    errors.append(f"{iid}: impressions: {exc}")
                    new = impressions.derived(analyses_by_id.get(iid))
        else:
            new = impressions.derived(analyses_by_id.get(iid))
        item["impression"] = impressions.carry_over(new, old)
    log.info("impressions: %d from transcripts", sum(
        1 for i in scored if i["impression"]["coverage"] == "transcript"))
    return errors


def refresh_insights(cfg: Settings, meetings_by_id: dict, llm: JsonLLM | None,
                     comp: dict | None, force_coach: bool = False) -> dict:
    """Without re-scoring: add interviewer impressions and the coach read to the existing
    performance.json and write it as schema v3 (+ the brief's Trend line). -> perf."""
    perf = read_json(cfg.performance_path)
    if not perf or not perf.get("interviews"):
        raise LLMError(f"{cfg.performance_path.name} has no interviews to refresh")
    record = read_json(cfg.evaluation_path) or {}
    analyses = {f"{a.get('source')}:{a.get('id')}": a for a in record.get("interviews") or []}
    scored = [dict(iv) for iv in perf["interviews"]]
    errors = attach_impressions(scored, meetings_by_id, analyses, perf, llm, cfg)
    new = performance.build(scored, perf.get("recurring_weak_topics") or [])
    new = performance.with_coach(new, llm, cfg.evaluator_model, cfg.candidate_name, comp,
                                 previous=None if force_coach else perf.get("coach"))
    new["errors"] = list(perf.get("errors") or []) + errors
    write_private(cfg.performance_path, redact_compensation(
        json.dumps(new, indent=2, ensure_ascii=False), comp) + "\n")
    write_private(cfg.performance_csv, redact_compensation(performance.to_csv(new), comp))
    if record.get("brief"):
        record["brief"] = dict(record["brief"], trend=new["trend_text"])
        record["brief_text"] = evaluate.fit_brief(record["brief"], len(record.get("interviews") or []))
        record["schema_version"] = rubric.SCHEMA_VERSION
        write_private(cfg.evaluation_path, redact_compensation(
            json.dumps(record, indent=2, ensure_ascii=False), comp) + "\n")
    return new


def load_comp(profile_path: Path) -> dict | None:
    try:
        return json.loads(profile_path.read_text(encoding="utf-8")).get("compensation")
    except (OSError, ValueError):
        return None
