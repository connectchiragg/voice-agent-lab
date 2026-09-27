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
            "SELECT role, content, status FROM messages WHERE conversation_id = %s ORDER BY id",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
    return [{"role": role, "parts": [{"text": content}], "status": status}
            for role, content, status in rows]


async def start_turn(conversation_id: str, message: str) -> int:
    # Default to interrupted until the stream finishes successfully.
    async with await connect() as connection:
        await connection.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
            (conversation_id, message),
        )
        cursor = await connection.execute(
            "INSERT INTO messages (conversation_id, role, content, status) "
            "VALUES (%s, 'model', '', 'interrupted') RETURNING id",
            (conversation_id,),
        )
        message_id = (await cursor.fetchone())[0]
        await connection.execute(
            "UPDATE conversations SET updated_at = now() WHERE id = %s",
            (conversation_id,),
        )
    return message_id


async def update_reply(conversation_id: str, message_id: int, text: str,
                       status: str = "interrupted") -> None:
    async with await connect() as connection:
        await connection.execute(
            "UPDATE messages SET content = %s, status = %s WHERE id = %s AND conversation_id = %s",
            (text, status, message_id, conversation_id),
        )
        await connection.execute(
            "UPDATE conversations SET updated_at = now() WHERE id = %s",
            (conversation_id,),
        )
