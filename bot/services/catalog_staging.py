"""Durable channel-media staging for the private `/addcharacter` workflow.

A broadcast-channel post has no reliable human ``from_user`` in the Telegram
Bot API. The channel handler therefore stores only the replied media and sends
a one-time-style private deep link. When an authorized catalog manager opens
that link, this service binds the job to that user and prevents another manager
from taking over the same in-progress add session.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from bot.db.pool import get_pool

STAGED_ADD_START_PREFIX = "addmedia_"
STAGED_ADD_TOKEN_BYTES = 18
STAGED_ADD_TTL_SECONDS = 24 * 60 * 60
VALID_STAGED_MEDIA_TYPES = frozenset({"photo", "video"})


@dataclass(frozen=True)
class StagedCatalogAddJob:
    """A trusted Telegram media reference waiting for private catalog metadata."""

    id: int
    token: str
    source_chat_id: int
    source_message_id: int
    file_id: str
    media_type: str
    status: str
    claimed_by: int | None = None


def _to_job(row) -> StagedCatalogAddJob:
    return StagedCatalogAddJob(
        id=int(row["id"]),
        token=str(row["token"]),
        source_chat_id=int(row["source_chat_id"]),
        source_message_id=int(row["source_message_id"]),
        file_id=str(row["file_id"]),
        media_type=str(row["media_type"]),
        status=str(row["status"]),
        claimed_by=int(row["claimed_by"]) if row["claimed_by"] is not None else None,
    )


def _valid_source_value(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value != 0


def _valid_user_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_message_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


async def create_or_get_staged_catalog_add_job(
    *,
    source_chat_id: int,
    source_message_id: int,
    file_id: str,
    media_type: str,
) -> StagedCatalogAddJob:
    """Return one unexpired pending/claimed job for an exact channel media post.

    The source-pair advisory lock and partial unique index make repeated channel
    commands idempotent. A still-claimed job is intentionally returned rather
    than reassigned, so another catalog admin cannot steal an active private
    workflow by replying to the same source media again.
    """
    normalized_file_id = str(file_id or "").strip()
    normalized_media_type = str(media_type or "").strip().casefold()
    if (
        not _valid_source_value(source_chat_id)
        or not _valid_message_id(source_message_id)
        or not normalized_file_id
        or normalized_media_type not in VALID_STAGED_MEDIA_TYPES
    ):
        raise ValueError("invalid staged catalog media")

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # PostgreSQL's two-key advisory form takes signed INT values. Hash
            # the BIGINT Telegram IDs as text so this remains safe for channels
            # and messages outside a 32-bit range.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))",
                str(source_chat_id),
                str(source_message_id),
            )
            current = await conn.fetchrow(
                """
                SELECT id, token, source_chat_id, source_message_id, file_id,
                       media_type, status, claimed_by
                FROM catalog_add_staging_jobs
                WHERE source_chat_id = $1
                  AND source_message_id = $2
                  AND status IN ('pending', 'claimed')
                  AND expires_at > NOW()
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                source_chat_id,
                source_message_id,
            )
            if current:
                return _to_job(current)

            # Expired pending/claimed rows must no longer block a fresh retry
            # for the same source image.
            await conn.execute(
                """
                UPDATE catalog_add_staging_jobs
                SET status = 'expired', resolved_at = NOW()
                WHERE source_chat_id = $1
                  AND source_message_id = $2
                  AND status IN ('pending', 'claimed')
                  AND expires_at <= NOW()
                """,
                source_chat_id,
                source_message_id,
            )

            for _attempt in range(4):
                token = secrets.token_urlsafe(STAGED_ADD_TOKEN_BYTES)
                created = await conn.fetchrow(
                    """
                    INSERT INTO catalog_add_staging_jobs (
                        token, source_chat_id, source_message_id, file_id,
                        media_type, expires_at
                    )
                    VALUES (
                        $1, $2, $3, $4, $5,
                        NOW() + ($6::INT * INTERVAL '1 second')
                    )
                    ON CONFLICT (token) DO NOTHING
                    RETURNING id, token, source_chat_id, source_message_id,
                              file_id, media_type, status, claimed_by
                    """,
                    token,
                    source_chat_id,
                    source_message_id,
                    normalized_file_id,
                    normalized_media_type,
                    STAGED_ADD_TTL_SECONDS,
                )
                if created:
                    return _to_job(created)
    raise RuntimeError("could not create staged catalog add token")


async def claim_staged_catalog_add_job(
    *,
    token: str,
    actor_id: int,
) -> tuple[StagedCatalogAddJob | None, str]:
    """Bind an unexpired channel-media job to one authorized private user."""
    normalized_token = str(token or "").strip()
    if not normalized_token or not _valid_user_id(actor_id):
        return None, "invalid"

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, token, source_chat_id, source_message_id, file_id,
                       media_type, status, claimed_by,
                       expires_at <= NOW() AS is_expired
                FROM catalog_add_staging_jobs
                WHERE token = $1
                FOR UPDATE
                """,
                normalized_token,
            )
            if not row:
                return None, "not_found"
            if bool(row["is_expired"]):
                if str(row["status"]) in {"pending", "claimed"}:
                    await conn.execute(
                        """
                        UPDATE catalog_add_staging_jobs
                        SET status = 'expired', resolved_at = NOW()
                        WHERE id = $1
                        """,
                        int(row["id"]),
                    )
                return None, "expired"

            status = str(row["status"])
            claimed_by = int(row["claimed_by"]) if row["claimed_by"] is not None else None
            if status == "completed":
                return None, "completed"
            if status != "pending" and not (status == "claimed" and claimed_by == actor_id):
                return None, "claimed_by_other"

            if status == "pending":
                row = await conn.fetchrow(
                    """
                    UPDATE catalog_add_staging_jobs
                    SET status = 'claimed', claimed_by = $2, claimed_at = NOW()
                    WHERE id = $1 AND status = 'pending'
                    RETURNING id, token, source_chat_id, source_message_id,
                              file_id, media_type, status, claimed_by
                    """,
                    int(row["id"]),
                    actor_id,
                )
                if not row:
                    return None, "unavailable"
            return _to_job(row), "ok"


async def release_staged_catalog_add_job(*, job_id: int, actor_id: int) -> bool:
    """Return a cancelled private session to pending state before its expiry."""
    if not _valid_message_id(job_id) or not _valid_user_id(actor_id):
        return False
    pool = get_pool()
    async with pool.acquire() as conn:
        released = await conn.fetchval(
            """
            UPDATE catalog_add_staging_jobs
            SET status = 'pending', claimed_by = NULL, claimed_at = NULL
            WHERE id = $1
              AND claimed_by = $2
              AND status = 'claimed'
              AND expires_at > NOW()
            RETURNING id
            """,
            job_id,
            actor_id,
        )
    return released is not None


async def complete_staged_catalog_add_job_in_transaction(
    conn,
    *,
    job_id: int,
    actor_id: int,
) -> bool:
    """Seal a staged job inside the same transaction as its catalog insert."""
    if not _valid_message_id(job_id) or not _valid_user_id(actor_id):
        return False
    completed = await conn.fetchval(
        """
        UPDATE catalog_add_staging_jobs
        SET status = 'completed', completed_at = NOW(), resolved_at = NOW()
        WHERE id = $1
          AND claimed_by = $2
          AND status = 'claimed'
          AND expires_at > NOW()
        RETURNING id
        """,
        job_id,
        actor_id,
    )
    return completed is not None


async def complete_staged_catalog_add_job(*, job_id: int, actor_id: int) -> bool:
    """Seal a staged media job outside another transaction when necessary."""
    pool = get_pool()
    async with pool.acquire() as conn:
        return await complete_staged_catalog_add_job_in_transaction(
            conn,
            job_id=job_id,
            actor_id=actor_id,
        )
