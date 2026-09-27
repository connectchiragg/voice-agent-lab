"""Small SQL operations for durable conversation history."""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env.docker")


async def connect():
    return await psycopg.AsyncConnection.connect(
        host="127.0.0.1",
        port=5432,
        dbname="voice_agent",
        user="voice_agent",
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=5,
        options="-c statement_timeout=5000",
    )


async def load_history(conversation_id: str) -> list[dict]:
    async with await connect() as connection:
        await connection.execute(
            "INSERT INTO conversations (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (conversation_id,),
        )
        cursor = await connection.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
    return [{"role": role, "parts": [{"text": content}]} for role, content in rows]


async def save_exchange(conversation_id: str, message: str, reply: str) -> None:
    # The connection context commits all three changes together, or rolls back on error.
    async with await connect() as connection:
        await connection.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
            (conversation_id, message),
        )
        await connection.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'model', %s)",
            (conversation_id, reply),
        )
        await connection.execute(
            "UPDATE conversations SET updated_at = now() WHERE id = %s",
            (conversation_id,),
        )
