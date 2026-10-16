"""`python -m app.notetakers pending`: recordings waiting for the evaluation hook (no PII:
provider, id, date, status). `python -m app.notetakers handoff`: retry the hand-off."""
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
    if command not in ("pending", "handoff"):
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(command)))
