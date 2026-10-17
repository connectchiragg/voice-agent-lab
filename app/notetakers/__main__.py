"""`python -m app.notetakers pending`: recordings waiting for the evaluation hook (no PII:
provider, id, date, status). `python -m app.notetakers handoff`: retry the hand-off.
`python -m app.notetakers details`: fill company and interviewers of stored Fathom recordings
from Fathom's calendar invitees (last 180 days; prints counts only).
`python -m app.notetakers history --days 90 [--dry-run] [--yes] [--no-calendar]`: import and
quietly score every interview of the last N days (app/notetakers/history.py; run it in the
research worker container)."""
import asyncio
import sys

from app.notetakers import pipeline, store


async def main(command: str) -> int:
    from app import agent, research
    async with research.lifespan(agent.postgres_conninfo):
        if research.pool is None:
            print("Postgres is unavailable.")
            return 1
        async with research.pool.connection() as connection:
            await store.setup(connection)
            if command == "details":
                from app import notetakers
                async with notetakers.http_client() as http:
                    print(await notetakers.backfill_details(connection, http))
                return 0
            rows = await store.pending(connection, limit=100)
            for row in rows:
                date = row["started_at"].date().isoformat() if row["started_at"] else "-"
                print(f"{row['provider']}:{row['external_id']}  {date}  {row['eval_status']}")
                if command == "handoff":
                    print("  ->", await pipeline.hand_off(connection, row["provider"], row["external_id"], None))
            print(f"{len(rows)} waiting.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "pending"
    if command == "history":
        from app.notetakers import history
        sys.exit(history.cli(sys.argv[2:]))
    if command not in ("pending", "handoff", "details"):
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(command)))
