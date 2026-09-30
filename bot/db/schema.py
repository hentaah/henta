SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS characters (
    id            BIGINT PRIMARY KEY,
    name          TEXT NOT NULL,
    anime         TEXT NOT NULL,
    rarity        TEXT NOT NULL,
    rarity_score  INT  NOT NULL,
    aliases       TEXT NOT NULL,
    -- Historic name retained for compatibility: this is the Telegram file ID
    -- for either the card photo or its per-character video.
    image_url     TEXT NOT NULL,
    media_type    TEXT NOT NULL DEFAULT 'photo'
                  CHECK (media_type IN ('photo', 'video')),
    -- A blank event means this catalog card is not part of an event.
    event         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_characters_rarity ON characters (rarity);
CREATE INDEX IF NOT EXISTS idx_characters_anime  ON characters (anime);

-- Event labels are curated by catalog admins.  Character rows store the
-- selected label directly so a historical card remains readable if an event
-- label is later removed or renamed externally.
CREATE TABLE IF NOT EXISTS catalog_events (
    id            BIGSERIAL PRIMARY KEY,
    label         TEXT NOT NULL,
    -- A full owner can pause all future cards linked to one event without
    -- deleting either the event label or any catalog card.
    spawn_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by    BIGINT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_events_unique_label
ON catalog_events ((LOWER(BTRIM(label))));

-- Event controls above apply to named catalog events. Rarity controls below
-- are deliberately separate: a Full Owner can pause every future card in one
-- tier without modifying event labels, cards, or a different tier's policy.
-- A missing row means that rarity remains spawnable, including cards added
-- after the control was first created.
CREATE TABLE IF NOT EXISTS catalog_rarity_spawn_controls (
    rarity_key     TEXT PRIMARY KEY,
    label          TEXT NOT NULL,
    spawn_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (BTRIM(rarity_key) <> '')
);

-- Safe migrations for catalog tables created before event/media support.
ALTER TABLE characters ADD COLUMN IF NOT EXISTS event TEXT NOT NULL DEFAULT '';
-- Existing catalog file IDs were all Telegram photos.  New cards may opt into
-- video individually, without changing legacy character media.
ALTER TABLE characters ADD COLUMN IF NOT EXISTS media_type TEXT;
UPDATE characters
SET media_type = 'photo'
WHERE media_type IS NULL OR BTRIM(media_type) NOT IN ('photo', 'video');
ALTER TABLE characters ALTER COLUMN media_type SET DEFAULT 'photo';
ALTER TABLE characters ALTER COLUMN media_type SET NOT NULL;
ALTER TABLE characters DROP CONSTRAINT IF EXISTS characters_media_type_check;
ALTER TABLE characters ADD CONSTRAINT characters_media_type_check
    CHECK (media_type IN ('photo', 'video'));
ALTER TABLE catalog_events ADD COLUMN IF NOT EXISTS spawn_enabled BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS groups (
    chat_id          BIGINT PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    username         TEXT,
    -- HentaCatcher GP uses 100 messages; every other new group defaults to 125.
    spawn_interval   INT  NOT NULL DEFAULT 125,
    -- True only after a Full Owner explicitly uses /changemessage for this group.
    spawn_interval_custom BOOLEAN NOT NULL DEFAULT FALSE,
    claim_window     INT  NOT NULL DEFAULT 90,
    -- Durable, per-group automatic-spawn message progress. Redis may cache
    -- active cards but must not be the only source of spawn-counter state.
    msg_counter      INT  NOT NULL DEFAULT 0,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- The most recent five successfully spawned character IDs for this
    -- group. It prevents immediate repeat spawns across bot restarts.
    recent_spawn_ids BIGINT[] NOT NULL DEFAULT ARRAY[]::BIGINT[],
    -- Main-group OverLord cadence: successfully emitted lower-rank automatic
    -- cards since the last OverLord. It caps at 45 while no eligible OverLord
    -- catalog card exists, so adding one later makes the next event eligible.
    overlord_lower_spawn_count INT NOT NULL DEFAULT 0,
    overlord_countdown_announced BOOLEAN NOT NULL DEFAULT FALSE,
    -- Outside the main group, 70 automatic cards make the next eligible
    -- automatic spawn a Suspicious card.
    suspicious_lower_spawn_count INT NOT NULL DEFAULT 0,
    -- Main-group Suspicious cadence is intentionally separate from the
    -- OverLord cadence: every automatic Main Group spawn advances this count.
    main_suspicious_spawn_count INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_groups_enabled ON groups (enabled);

-- Spawn cadence is message-only. Remove obsolete time-trigger state from
-- databases created by older releases without touching group memberships.
ALTER TABLE groups DROP COLUMN IF EXISTS spawn_time_floor;
ALTER TABLE groups DROP COLUMN IF EXISTS last_spawn_at;
-- Existing v8 rows used the former shared 100-message default.  A nullable
-- first migration distinguishes those old defaults from prior non-100 owner
-- overrides before applying the requested target-group/default policy.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS spawn_interval_custom BOOLEAN;
UPDATE groups
SET spawn_interval_custom = (COALESCE(spawn_interval, 100) <> 100)
WHERE spawn_interval_custom IS NULL;
UPDATE groups
SET spawn_interval = CASE
    WHEN chat_id = -1003769071399 THEN 100
    ELSE 125
END
WHERE spawn_interval_custom = FALSE;
ALTER TABLE groups ALTER COLUMN spawn_interval SET DEFAULT 125;
ALTER TABLE groups ALTER COLUMN spawn_interval_custom SET DEFAULT FALSE;
ALTER TABLE groups ALTER COLUMN spawn_interval_custom SET NOT NULL;
-- Keep a persistent per-group anti-repeat queue for the last five spawns.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS recent_spawn_ids BIGINT[]
    NOT NULL DEFAULT ARRAY[]::BIGINT[];
-- Durable special-rank cadence counters. Existing groups start at zero, so an
-- upgrade cannot unexpectedly trigger an immediate OverLord/Suspicious spawn.
ALTER TABLE groups ADD COLUMN IF NOT EXISTS overlord_lower_spawn_count INT NOT NULL DEFAULT 0;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS overlord_countdown_announced BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS suspicious_lower_spawn_count INT NOT NULL DEFAULT 0;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS main_suspicious_spawn_count INT NOT NULL DEFAULT 0;
UPDATE groups
SET overlord_lower_spawn_count = GREATEST(COALESCE(overlord_lower_spawn_count, 0), 0),
    suspicious_lower_spawn_count = GREATEST(COALESCE(suspicious_lower_spawn_count, 0), 0),
    main_suspicious_spawn_count = GREATEST(COALESCE(main_suspicious_spawn_count, 0), 0),
    overlord_countdown_announced = COALESCE(overlord_countdown_announced, FALSE);

CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    username       TEXT,
    full_name      TEXT NOT NULL DEFAULT '',
    language_code  TEXT,
    total_claims         INT NOT NULL DEFAULT 0,
    -- Fixed global 9-hour /catch window. The key is floor(epoch / 32400),
    -- so every player resets together without a background reset job.
    catch_window_key     BIGINT NOT NULL DEFAULT -1,
    catch_window_claims  INT NOT NULL DEFAULT 0,
    -- Per-player rolling /daily cooldown. It is NULL until the first reward.
    daily_claimed_at     TIMESTAMPTZ,
    -- Independent per-player rolling /dailyneko wallet-reward cooldown.
    daily_neko_claimed_at TIMESTAMPTZ,
    -- Legacy Neko game/pass state is retained for historic records after the
    -- public /game command retirement. Two fractional digits retain payable
    -- casino cents after Mines multipliers.
    neko_balance                 NUMERIC(21, 2) NOT NULL DEFAULT 0.00 CHECK (neko_balance >= 0),
    neko_game_window_started_at  TIMESTAMPTZ,
    neko_game_uses               INT NOT NULL DEFAULT 0 CHECK (neko_game_uses >= 0),
    neko_pass_expires_at         TIMESTAMPTZ,
    -- Each successful /casino start begins this player's independent 70-second
    -- start cooldown. The legacy counter remains at zero for safe upgrades.
    casino_starts_since_cooldown SMALLINT NOT NULL DEFAULT 0,
    casino_cooldown_until        TIMESTAMPTZ,
    casino_cooldown_version      SMALLINT NOT NULL DEFAULT 2,
    highest_rarity       TEXT,
    is_banned            BOOLEAN NOT NULL DEFAULT FALSE,
    spam_banned_until    TIMESTAMPTZ,
    -- Full-owner `/mute <days>` is an explicit bot-wide mute. It is separate
    -- from the short automatic anti-spam pause so it blocks every bot surface.
    manual_muted_until   TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A Boom is a durable permanent bot ban with an owner-supplied reason. The
-- separate record preserves moderation context without overloading normal
-- temporary-spam/manual-mute state in `users`.
CREATE TABLE IF NOT EXISTS boom_bans (
    user_id          BIGINT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    reason           TEXT NOT NULL CHECK (BTRIM(reason) <> ''),
    boomed_by_user_id BIGINT NOT NULL,
    boomed_by_name   TEXT NOT NULL DEFAULT '',
    boomed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_boom_bans_boomed_at
ON boom_bans (boomed_at DESC, user_id ASC);

-- A limited OverLord card can be caught by at most ten distinct people over
-- its lifetime. This durable claim ledger intentionally remains after a holder
-- burns/resets their Harem copy, so a completed card never returns to random
-- spawning. Global character deletion cascades it away with the catalog row.
CREATE TABLE IF NOT EXISTS overlord_claims (
    character_id BIGINT NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
    user_id      BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    claimed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (character_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_overlord_claims_character
ON overlord_claims (character_id, claimed_at ASC);

-- Global, durable collection milestones. A rarity record can carry one chosen
-- next-tier reward; anime/game records deliberately have no character reward.
-- The key is normalized by the service (for example rarity:common or
-- anime:re:zero), so a collection remains unlocked even if cards are later
-- traded away or a title is renamed.
CREATE TABLE IF NOT EXISTS user_achievements (
    user_id             BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    achievement_key     TEXT NOT NULL,
    achievement_type    TEXT NOT NULL CHECK (achievement_type IN ('rarity', 'anime')),
    rarity              TEXT,
    anime               TEXT,
    unlocked_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reward_claimed_at   TIMESTAMPTZ,
    reward_character_id BIGINT REFERENCES characters (id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, achievement_key)
);
CREATE INDEX IF NOT EXISTS idx_user_achievements_user_type
ON user_achievements (user_id, achievement_type);

CREATE TABLE IF NOT EXISTS active_spawns (
    chat_id      BIGINT NOT NULL,
    character_id BIGINT NOT NULL REFERENCES characters (id),
    message_id   BIGINT NOT NULL,
    -- Fractional seconds make the mandatory four-second catch gate exact.
    spawned_at   DOUBLE PRECISION NOT NULL,
    expires_at   BIGINT NOT NULL,
    -- Only populated when this current card replaced an earlier, still-live
    -- card in the same group. It supports clear feedback for an old /catch.
    superseded_character_id BIGINT REFERENCES characters (id) ON DELETE SET NULL,
    superseded_at DOUBLE PRECISION,
    PRIMARY KEY (chat_id)
);
-- Existing installations stored whole seconds. Preserve their values while
-- promoting this timestamp so new spawns can enforce a full four seconds.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'active_spawns'
          AND column_name = 'spawned_at'
          AND data_type <> 'double precision'
    ) THEN
        ALTER TABLE active_spawns
        ALTER COLUMN spawned_at TYPE DOUBLE PRECISION USING spawned_at::DOUBLE PRECISION;
    END IF;
END $$;
-- Safe migration for installations created before continuous replacement
-- spawns tracked their immediately superseded active card.
ALTER TABLE active_spawns
ADD COLUMN IF NOT EXISTS superseded_character_id BIGINT
REFERENCES characters (id) ON DELETE SET NULL;
ALTER TABLE active_spawns
ADD COLUMN IF NOT EXISTS superseded_at DOUBLE PRECISION;
CREATE INDEX IF NOT EXISTS idx_active_spawns_expires ON active_spawns (expires_at);

-- An automatic OP card is selected and publicly announced first, then held
-- here for its one-minute delay.  One row per group makes the announcement
-- authoritative across restarts and prevents duplicate OP posts.
CREATE TABLE IF NOT EXISTS scheduled_op_spawns (
    chat_id          BIGINT PRIMARY KEY,
    character_id     BIGINT NOT NULL REFERENCES characters (id),
    claim_window     INT NOT NULL CHECK (claim_window > 0),
    spawn_at         DOUBLE PRECISION NOT NULL,
    -- A scheduler lease survives a process crash. Stale leases expire and let
    -- the selected, already-announced OP card be retried after restart.
    processing_token TEXT,
    processing_until DOUBLE PRECISION
);
-- Safe for an early package install that created the queue before leases were
-- added. The current archive creates all three fields in the table above.
ALTER TABLE scheduled_op_spawns ADD COLUMN IF NOT EXISTS processing_token TEXT;
ALTER TABLE scheduled_op_spawns ADD COLUMN IF NOT EXISTS processing_until DOUBLE PRECISION;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'scheduled_op_spawns'
          AND column_name = 'spawn_at'
          AND data_type <> 'double precision'
    ) THEN
        ALTER TABLE scheduled_op_spawns
        ALTER COLUMN spawn_at TYPE DOUBLE PRECISION USING spawn_at::DOUBLE PRECISION;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_scheduled_op_spawns_due
ON scheduled_op_spawns (spawn_at);

CREATE TABLE IF NOT EXISTS harem (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users (user_id),
    chat_id      BIGINT NOT NULL,
    character_id BIGINT NOT NULL REFERENCES characters (id),
    is_favorite  BOOLEAN NOT NULL DEFAULT FALSE,
    -- /sudo all - cards are intentionally visible inventory but never
    -- contribute to ranking, group stats, or catcher reports.
    is_ranked    BOOLEAN NOT NULL DEFAULT TRUE,
    acquired_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_harem_user_chat ON harem (user_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_harem_chat      ON harem (chat_id);
CREATE INDEX IF NOT EXISTS idx_harem_character ON harem (character_id);
CREATE INDEX IF NOT EXISTS idx_harem_user_character ON harem (user_id, character_id);

-- Exact successful /catch events are kept separately from Harem ownership.
-- This prevents /daily, /sudo, Market transfers, gifts, restores, burns, and
-- later Harem deletion from changing rolling catch-only leaderboards. The
-- claim service retains eight days because `/catchtop` needs only 24 hours and
-- seven days; its time-first index keeps the public Main Group query small.
CREATE TABLE IF NOT EXISTS catch_events (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id      BIGINT NOT NULL,
    character_id BIGINT REFERENCES characters (id) ON DELETE SET NULL,
    caught_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_catch_events_chat_caught_user
ON catch_events (chat_id, caught_at DESC, user_id);

-- A broadcast channel does not expose the human sender of a channel post. A
-- reply-to-media `/addcharacter` therefore creates one short-lived staged job
-- and lets an authorized catalog manager claim it through an owner-safe private
-- deep link. The source media remains Telegram-hosted; no binary is copied.
CREATE TABLE IF NOT EXISTS catalog_add_staging_jobs (
    id                BIGSERIAL PRIMARY KEY,
    token             TEXT NOT NULL UNIQUE,
    source_chat_id    BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    file_id           TEXT NOT NULL,
    media_type        TEXT NOT NULL CHECK (media_type IN ('photo', 'video')),
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'claimed', 'completed', 'cancelled', 'expired')),
    claimed_by        BIGINT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ,
    expires_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catalog_add_staging_token_status
ON catalog_add_staging_jobs (token, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_add_staging_active_source
ON catalog_add_staging_jobs (source_chat_id, source_message_id)
WHERE status IN ('pending', 'claimed');
CREATE INDEX IF NOT EXISTS idx_catalog_add_staging_expiry
ON catalog_add_staging_jobs (expires_at);

-- Love is a catalog-only, non-spawnable relationship tier.  Its public L-code
-- is deliberately independent from ordinary numeric catalog IDs, and every
-- card has exactly five owner-provided photo levels.
CREATE SEQUENCE IF NOT EXISTS love_card_code_sequence START WITH 1;

CREATE TABLE IF NOT EXISTS love_cards (
    character_id BIGINT PRIMARY KEY REFERENCES characters (id) ON DELETE CASCADE,
    love_code    BIGINT NOT NULL UNIQUE CHECK (love_code > 0),
    base_price   NUMERIC(21, 2) NOT NULL CHECK (base_price > 0),
    created_by   BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_love_cards_code ON love_cards (love_code);

CREATE TABLE IF NOT EXISTS love_card_levels (
    character_id BIGINT NOT NULL REFERENCES love_cards (character_id) ON DELETE CASCADE,
    level        SMALLINT NOT NULL CHECK (level BETWEEN 1 AND 5),
    image_url    TEXT NOT NULL,
    media_type   TEXT NOT NULL DEFAULT 'photo' CHECK (media_type = 'photo'),
    PRIMARY KEY (character_id, level)
);
CREATE INDEX IF NOT EXISTS idx_love_card_levels_media ON love_card_levels (image_url);

-- An owner may grant several Love cards, but one selected non-escrow card is
-- the target of no-argument actions.  A listing preserves its owner state in
-- escrow so cancellation restores its existing level/points safely.
CREATE TABLE IF NOT EXISTS love_player_states (
    user_id                BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    character_id           BIGINT NOT NULL REFERENCES love_cards (character_id) ON DELETE CASCADE,
    level                  SMALLINT NOT NULL DEFAULT 1 CHECK (level BETWEEN 1 AND 5),
    level_points           INT NOT NULL DEFAULT 0 CHECK (level_points >= 0),
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    is_escrowed            BOOLEAN NOT NULL DEFAULT FALSE,
    kiss_cooldown_until    TIMESTAMPTZ,
    hug_cooldown_until     TIMESTAMPTZ,
    date_cooldown_until    TIMESTAMPTZ,
    romance_cooldown_until TIMESTAMPTZ,
    acquired_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, character_id),
    CHECK (NOT is_active OR NOT is_escrowed)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_love_player_one_active
ON love_player_states (user_id)
WHERE is_active = TRUE AND is_escrowed = FALSE;
CREATE INDEX IF NOT EXISTS idx_love_player_states_character
ON love_player_states (character_id, user_id);

-- Per-user display choice for /harem. Anime is the default; rarity mode
-- stores the selected tier and is configured only through /henmode.
CREATE TABLE IF NOT EXISTS harem_display_preferences (
    user_id    BIGINT PRIMARY KEY,
    display_mode TEXT NOT NULL DEFAULT 'anime',
    rarity     TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Migration: duplicate catches are now allowed (a user can own the same
-- character more than once), so the old one-copy-per-character UNIQUE
-- constraint is dropped if it exists from a previous schema version.
ALTER TABLE harem DROP CONSTRAINT IF EXISTS harem_user_id_chat_id_character_id_key;

-- Migrations for databases created before these fields existed.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS spam_banned_until TIMESTAMPTZ;
-- Manual full-owner mutes have their own durable deadline. Unlike the
-- anti-spam pause, they block every member-facing bot surface until /unmute.
ALTER TABLE users ADD COLUMN IF NOT EXISTS manual_muted_until TIMESTAMPTZ;
-- Catch-limit state is deliberately stored on the user row. A new globally
-- aligned window key resets the effective count lazily for all users at once.
ALTER TABLE users ADD COLUMN IF NOT EXISTS catch_window_key BIGINT NOT NULL DEFAULT -1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS catch_window_claims INT NOT NULL DEFAULT 0;
-- `/daily` uses a rolling 24-hour per-player timestamp, not a global reset.
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_claimed_at TIMESTAMPTZ;
-- `/dailyneko` has an independent personal rolling-24-hour wallet reward.
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_neko_claimed_at TIMESTAMPTZ;
-- Neko wallet state is additive, so established collections keep their users
-- and simply begin with a zero balance. Retired game/pass columns remain for
-- non-destructive historic compatibility.
ALTER TABLE users ADD COLUMN IF NOT EXISTS neko_balance NUMERIC(21, 2) NOT NULL DEFAULT 0.00
    CHECK (neko_balance >= 0);
ALTER TABLE users ADD COLUMN IF NOT EXISTS neko_game_window_started_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS neko_game_uses INT NOT NULL DEFAULT 0
    CHECK (neko_game_uses >= 0);
ALTER TABLE users ADD COLUMN IF NOT EXISTS neko_pass_expires_at TIMESTAMPTZ;
-- `/casino` now has one durable 70-second personal start cooldown after
-- every board. The retired batch counter is retained as zero-only compatibility
-- state so an older reset/backup path cannot fail on a missing column.
ALTER TABLE users ADD COLUMN IF NOT EXISTS casino_starts_since_cooldown SMALLINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS casino_cooldown_until TIMESTAMPTZ;
-- A nullable version is intentional here: a v8 database receives version 1,
-- letting runtime discard an already-running former five-minute pause exactly
-- once rather than forcing it to finish after this update.
ALTER TABLE users ADD COLUMN IF NOT EXISTS casino_cooldown_version SMALLINT;
UPDATE users
SET casino_starts_since_cooldown = 0
WHERE casino_starts_since_cooldown IS NULL;
UPDATE users
SET casino_cooldown_version = 1
WHERE casino_cooldown_version IS NULL;
ALTER TABLE users ALTER COLUMN casino_starts_since_cooldown SET DEFAULT 0;
ALTER TABLE users ALTER COLUMN casino_starts_since_cooldown SET NOT NULL;
ALTER TABLE users ALTER COLUMN casino_cooldown_version SET DEFAULT 2;
ALTER TABLE users ALTER COLUMN casino_cooldown_version SET NOT NULL;
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_casino_starts_batch_check;
ALTER TABLE users ADD CONSTRAINT users_casino_starts_batch_check
    CHECK (casino_starts_since_cooldown >= 0);
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_casino_cooldown_version_check;
ALTER TABLE users ADD CONSTRAINT users_casino_cooldown_version_check
    CHECK (casino_cooldown_version IN (1, 2));
-- Serves deterministic Neko-balance leaderboard rank reads.
CREATE INDEX IF NOT EXISTS idx_users_neko_balance_rank
ON users (neko_balance DESC, user_id ASC);
ALTER TABLE harem ADD COLUMN IF NOT EXISTS is_favorite BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE harem ADD COLUMN IF NOT EXISTS is_ranked BOOLEAN NOT NULL DEFAULT TRUE;
CREATE INDEX IF NOT EXISTS idx_harem_ranked_character
ON harem (character_id, user_id) WHERE is_ranked = TRUE;

-- Migration: older versions allowed one favorite per group.  Keep the most
-- recently acquired selection and clear the rest so a user has one global
-- showcase character across the bot.
WITH ranked_global_favorites AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY user_id
               ORDER BY acquired_at DESC, id DESC
           ) AS rn
    FROM harem
    WHERE is_favorite = TRUE
)
UPDATE harem AS h
SET is_favorite = FALSE
FROM ranked_global_favorites AS r
WHERE h.id = r.id AND r.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_harem_one_global_favorite
ON harem (user_id)
WHERE is_favorite = TRUE;

-- Persistent delegated roles. The configured OWNER_ID remains the root owner;
-- /upowner may add full owners and /upadmin may add catalog-only admins.
CREATE TABLE IF NOT EXISTS bot_owners (
    user_id    BIGINT PRIMARY KEY,
    granted_by BIGINT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_admins (
    user_id    BIGINT PRIMARY KEY,
    granted_by BIGINT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One persistent global maintenance switch.  It intentionally has no foreign
-- keys so it is available before a user record is created.
CREATE TABLE IF NOT EXISTS bot_runtime_settings (
    singleton  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    is_killed  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Full Owners can opt in to current-media catalog change audit posts.
    database_add_log_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    changed_by BIGINT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE bot_runtime_settings
ADD COLUMN IF NOT EXISTS database_add_log_enabled BOOLEAN NOT NULL DEFAULT FALSE;
INSERT INTO bot_runtime_settings (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

-- A root-only /deletedatabase confirmation is persisted and single-use, so an
-- old or double-clicked Telegram button can never trigger a second wipe later.
-- This is an audit/confirmation record, never a player-data backup.
CREATE TABLE IF NOT EXISTS player_database_reset_jobs (
    id           BIGSERIAL PRIMARY KEY,
    owner_id     BIGINT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    resolved_at  TIMESTAMPTZ,
    error_detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_player_database_reset_jobs_pending_owner
ON player_database_reset_jobs (owner_id, status, created_at DESC);

-- Full Owner `/deletefromharem <codes>` jobs keep a multi-code permanent
-- player-inventory purge confirmation durable across restarts. They create no
-- recovery backup: selected player copies, Love state, escrow, and stale
-- pending transfers are deliberately removed after confirmation.
CREATE TABLE IF NOT EXISTS global_harem_purge_jobs (
    id                         BIGSERIAL PRIMARY KEY,
    owner_id                   BIGINT NOT NULL,
    character_ids              BIGINT[] NOT NULL CHECK (cardinality(character_ids) > 0),
    status                     TEXT NOT NULL DEFAULT 'pending'
                               CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at               TIMESTAMPTZ,
    resolved_at                TIMESTAMPTZ,
    harem_copies_removed       INT NOT NULL DEFAULT 0 CHECK (harem_copies_removed >= 0),
    love_states_removed        INT NOT NULL DEFAULT 0 CHECK (love_states_removed >= 0),
    player_listings_removed    INT NOT NULL DEFAULT 0 CHECK (player_listings_removed >= 0),
    backup_items_removed       INT NOT NULL DEFAULT 0 CHECK (backup_items_removed >= 0),
    pending_trades_failed      INT NOT NULL DEFAULT 0 CHECK (pending_trades_failed >= 0),
    pending_gifts_failed       INT NOT NULL DEFAULT 0 CHECK (pending_gifts_failed >= 0),
    pending_sudo_grants_failed INT NOT NULL DEFAULT 0 CHECK (pending_sudo_grants_failed >= 0),
    error_detail               TEXT
);
CREATE INDEX IF NOT EXISTS idx_global_harem_purge_jobs_pending_owner
ON global_harem_purge_jobs (owner_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id                BIGSERIAL PRIMARY KEY,
    chat_id           BIGINT NOT NULL,
    from_user_id      BIGINT NOT NULL,
    to_user_id        BIGINT NOT NULL,
    from_character_id BIGINT NOT NULL REFERENCES characters (id),
    to_character_id   BIGINT NOT NULL REFERENCES characters (id),
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS gifts (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       BIGINT NOT NULL,
    from_user_id  BIGINT NOT NULL,
    to_user_id    BIGINT NOT NULL,
    character_ids    BIGINT[] NOT NULL,
    -- Exact source rows make a confirmed gift deterministic and preserve the
    -- per-copy unranked marker when a harem contains mixed inventory.
    source_harem_ids BIGINT[],
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_gifts_pending_sender
ON gifts (from_user_id, status);
CREATE INDEX IF NOT EXISTS idx_gifts_pending_recipient
ON gifts (to_user_id, status);
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS source_harem_ids BIGINT[];

-- Confirmation-safe Neko transfers.  A request does not reserve currency;
-- the sender's current balance is locked and rechecked only when that sender
-- presses Confirm, so stale prompts cannot overdraft a wallet.
CREATE TABLE IF NOT EXISTS neko_transfer_requests (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       BIGINT NOT NULL,
    from_user_id  BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    to_user_id    BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    amount        NUMERIC(21, 2) NOT NULL CHECK (amount > 0),
    CHECK (from_user_id <> to_user_id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'accepted', 'cancelled', 'failed')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_neko_transfers_pending_sender
ON neko_transfer_requests (from_user_id, status, created_at DESC)
WHERE status = 'pending';

-- A compact immutable audit trail accompanies every wallet balance change.
-- It is intentionally internal for now, but makes owner resets/restores and
-- user-confirmed transfers independently traceable.
CREATE TABLE IF NOT EXISTS neko_wallet_ledger (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    delta           NUMERIC(21, 2) NOT NULL,
    balance_after   NUMERIC(21, 2) NOT NULL CHECK (balance_after >= 0),
    reason          TEXT NOT NULL,
    related_user_id BIGINT,
    reference_id    BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_neko_wallet_ledger_user
ON neko_wallet_ledger (user_id, created_at DESC, id DESC);

-- Retired UTC streak records and milestones remain durable for historic
-- compatibility. They are no longer advanced or exposed by Daily Neko.
CREATE TABLE IF NOT EXISTS neko_daily_streaks (
    user_id                    BIGINT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    streak_count               INT NOT NULL DEFAULT 0 CHECK (streak_count >= 0),
    last_claim_date            DATE,
    day3_rewarded_at           TIMESTAMPTZ,
    day7_rewarded_at           TIMESTAMPTZ,
    day30_badge_unlocked_at    TIMESTAMPTZ,
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Retired UTC weekly-quest records are retained without destructive migration.
-- New gameplay no longer advances or exposes these rows.
CREATE TABLE IF NOT EXISTS neko_weekly_quests (
    user_id                          BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    week_start                       DATE NOT NULL,
    catch_count                      INT NOT NULL DEFAULT 0 CHECK (catch_count >= 0),
    daily_neko_dates                 DATE[] NOT NULL DEFAULT ARRAY[]::DATE[],
    game_count                       INT NOT NULL DEFAULT 0 CHECK (game_count >= 0),
    gift_count                       INT NOT NULL DEFAULT 0 CHECK (gift_count >= 0),
    catch_reward_claimed_at          TIMESTAMPTZ,
    daily_neko_reward_claimed_at     TIMESTAMPTZ,
    game_reward_claimed_at           TIMESTAMPTZ,
    gift_reward_claimed_at           TIMESTAMPTZ,
    completion_reward_claimed_at     TIMESTAMPTZ,
    created_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, week_start),
    CHECK (week_start = date_trunc('week', week_start::TIMESTAMP)::DATE)
);
CREATE INDEX IF NOT EXISTS idx_neko_weekly_quests_user_week
ON neko_weekly_quests (user_id, week_start DESC);

-- /deletewallet stores a balance-only snapshot.  /backwallet consumes the newest unused
-- one and replaces the current balance rather than adding it to the wallet.
CREATE TABLE IF NOT EXISTS neko_wallet_deletion_backups (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    deleted_by       BIGINT NOT NULL,
    original_balance NUMERIC(21, 2) NOT NULL CHECK (original_balance >= 0),
    deleted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_by      BIGINT,
    restored_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_neko_wallet_backups_pending_user
ON neko_wallet_deletion_backups (user_id, deleted_at DESC, id DESC)
WHERE restored_at IS NULL;

-- Pass expiry on users is the live entitlement; this short history records
-- which full owner activated or extended it and by how many custom days.
CREATE TABLE IF NOT EXISTS neko_pass_grants (
    id                  BIGSERIAL PRIMARY KEY,
    user_id             BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    granted_by          BIGINT NOT NULL,
    days                INT NOT NULL CHECK (days > 0),
    previous_expires_at TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ NOT NULL,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_neko_pass_grants_user
ON neko_pass_grants (user_id, granted_at DESC, id DESC);

-- Every player can hold one independent active Neko Mines board; there is
-- deliberately no global active-game gate. New boards are 3×3 with one bomb.
-- The rule-compatible constraints also retain any older 4×4 / three-bomb
-- board until it resolves, so an upgrade cannot strand its reserved stake.
CREATE TABLE IF NOT EXISTS neko_casino_games (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id        BIGINT NOT NULL,
    stake          NUMERIC(21, 2) NOT NULL CHECK (stake > 0),
    bomb_positions SMALLINT[] NOT NULL,
    safe_positions SMALLINT[] NOT NULL DEFAULT ARRAY[]::SMALLINT[],
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'cashed_out', 'lost', 'cancelled')),
    payout         NUMERIC(21, 2),
    -- Stores the exact payout schedule used when this board was started.
    -- Version 1 is historic linear x0.375, version 2 is historic balanced,
    -- and version 3 is the current gradual x1-first-safe/x3-maximum table.
    payout_version SMALLINT NOT NULL DEFAULT 3
                   CHECK (payout_version IN (1, 2, 3)),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ,
    CONSTRAINT neko_casino_bomb_positions_rules_check CHECK (
        (cardinality(bomb_positions) = 1
         AND bomb_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8]::SMALLINT[])
        OR
        (cardinality(bomb_positions) = 3
         AND bomb_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]::SMALLINT[])
    ),
    CONSTRAINT neko_casino_safe_positions_rules_check CHECK (
        (cardinality(bomb_positions) = 1
         AND cardinality(safe_positions) <= 8
         AND safe_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8]::SMALLINT[])
        OR
        (cardinality(bomb_positions) = 3
         AND cardinality(safe_positions) <= 13
         AND safe_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]::SMALLINT[])
    ),
    CONSTRAINT neko_casino_safe_not_bomb_check
        CHECK (NOT (safe_positions && bomb_positions))
);

-- Older packages created unnamed fixed-4×4 column checks. Replace only those
-- generated constraints with rule-compatible checks; existing active boards
-- remain valid and can finish under their original stored three-bomb rules.
DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    FOR old_constraint_name IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'neko_casino_games'::regclass
          AND contype = 'c'
          AND conname ~ '^neko_casino_games_(bomb_positions|safe_positions)_check[0-9]*$'
    LOOP
        EXECUTE format(
            'ALTER TABLE neko_casino_games DROP CONSTRAINT IF EXISTS %I',
            old_constraint_name
        );
    END LOOP;
END $$;
ALTER TABLE neko_casino_games
    DROP CONSTRAINT IF EXISTS neko_casino_bomb_positions_rules_check;
ALTER TABLE neko_casino_games
    DROP CONSTRAINT IF EXISTS neko_casino_safe_positions_rules_check;
ALTER TABLE neko_casino_games
    DROP CONSTRAINT IF EXISTS neko_casino_safe_not_bomb_check;
ALTER TABLE neko_casino_games
    ADD CONSTRAINT neko_casino_bomb_positions_rules_check CHECK (
        (cardinality(bomb_positions) = 1
         AND bomb_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8]::SMALLINT[])
        OR
        (cardinality(bomb_positions) = 3
         AND bomb_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]::SMALLINT[])
    );
ALTER TABLE neko_casino_games
    ADD CONSTRAINT neko_casino_safe_positions_rules_check CHECK (
        (cardinality(bomb_positions) = 1
         AND cardinality(safe_positions) <= 8
         AND safe_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8]::SMALLINT[])
        OR
        (cardinality(bomb_positions) = 3
         AND cardinality(safe_positions) <= 13
         AND safe_positions <@ ARRAY[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]::SMALLINT[])
    );
ALTER TABLE neko_casino_games
    ADD CONSTRAINT neko_casino_safe_not_bomb_check
    CHECK (NOT (safe_positions && bomb_positions));

-- Payout versions are immutable: historic pre-v15/v18–v30 linear boards use
-- version 1 and historic v15–v17 balanced boards use version 2. v31 adds the
-- gradual version-3 schedule for newly created boards only: its first safe
-- tile keeps payout at x1, then it rises to x3 after all eight safe tiles.
ALTER TABLE neko_casino_games
    ADD COLUMN IF NOT EXISTS payout_version SMALLINT;
UPDATE neko_casino_games
SET payout_version = 1
WHERE payout_version IS NULL;
ALTER TABLE neko_casino_games
    ALTER COLUMN payout_version SET DEFAULT 3;
ALTER TABLE neko_casino_games
    ALTER COLUMN payout_version SET NOT NULL;
ALTER TABLE neko_casino_games
    DROP CONSTRAINT IF EXISTS neko_casino_games_payout_version_check;
ALTER TABLE neko_casino_games
    ADD CONSTRAINT neko_casino_games_payout_version_check
    CHECK (payout_version IN (1, 2, 3));

-- Remove a pre-release global partial unique index if one exists. The only
-- supported active-game uniqueness is per user, so several players can start
-- in the same group at once without replacing/deactivating one another.
DO $$
DECLARE
    legacy_index_name TEXT;
BEGIN
    FOR legacy_index_name IN
        SELECT index_class.relname
        FROM pg_index AS index_meta
        JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
        WHERE index_meta.indrelid = 'neko_casino_games'::regclass
          AND index_meta.indisunique
          AND index_meta.indpred IS NOT NULL
          AND position('(user_id)' IN pg_get_indexdef(index_meta.indexrelid)) = 0
          AND COALESCE(
                pg_get_expr(index_meta.indpred, index_meta.indrelid),
                ''
              ) ILIKE '%status%active%'
          AND NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conindid = index_meta.indexrelid
              )
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %I', legacy_index_name);
    END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_neko_casino_one_active_user
ON neko_casino_games (user_id)
WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_neko_casino_user_status
ON neko_casino_games (user_id, status, created_at DESC);

-- `/casino` first records one reply-bound input request.  This
-- keeps Mine/Dice stake entry scoped to the initiating player and chat rather
-- than treating arbitrary group messages as wagers.
CREATE TABLE IF NOT EXISTS neko_casino_input_sessions (
    user_id           BIGINT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id           BIGINT NOT NULL,
    prompt_message_id BIGINT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('mine', 'dice')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_neko_casino_input_session_prompt
ON neko_casino_input_sessions (user_id, chat_id, prompt_message_id);

-- Dice bets reserve a validated stake only when a player chooses Odd, Even,
-- or one exact face.  A Telegram 🎲 reply then settles the server-stored bet.
CREATE TABLE IF NOT EXISTS neko_dice_bets (
    id                     BIGSERIAL PRIMARY KEY,
    user_id                BIGINT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    chat_id                BIGINT NOT NULL,
    stake                  NUMERIC(21, 2) NOT NULL CHECK (stake >= 50.00 AND stake <= 2000.00),
    choice                 TEXT CHECK (choice IN ('odd', 'even', '1', '2', '3', '4', '5', '6')),
    status                 TEXT NOT NULL DEFAULT 'selecting'
                           CHECK (status IN ('selecting', 'awaiting_roll', 'won', 'lost', 'cancelled')),
    roll_prompt_message_id BIGINT,
    dice_value             SMALLINT CHECK (dice_value BETWEEN 1 AND 6),
    payout                 NUMERIC(21, 2),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    selected_at            TIMESTAMPTZ,
    resolved_at            TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_neko_dice_one_active_user
ON neko_dice_bets (user_id)
WHERE status IN ('selecting', 'awaiting_roll');
CREATE INDEX IF NOT EXISTS idx_neko_dice_user_status
ON neko_dice_bets (user_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_neko_dice_roll_prompt
ON neko_dice_bets (user_id, chat_id, roll_prompt_message_id)
WHERE status = 'awaiting_roll';

-- The preceding Neko release stored integer currency. Promote existing values
-- exactly to two-decimal NUMERIC only once so casino payouts can be settled
-- without rounding away payable fractional Neko.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users'
          AND column_name = 'neko_balance'
          AND data_type <> 'numeric'
    ) THEN
        ALTER TABLE users
        ALTER COLUMN neko_balance TYPE NUMERIC(21, 2)
        USING neko_balance::NUMERIC(21, 2);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'neko_transfer_requests'
          AND column_name = 'amount'
          AND data_type <> 'numeric'
    ) THEN
        ALTER TABLE neko_transfer_requests
        ALTER COLUMN amount TYPE NUMERIC(21, 2)
        USING amount::NUMERIC(21, 2);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'neko_wallet_ledger'
          AND column_name = 'delta'
          AND data_type <> 'numeric'
    ) THEN
        ALTER TABLE neko_wallet_ledger
        ALTER COLUMN delta TYPE NUMERIC(21, 2)
        USING delta::NUMERIC(21, 2);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'neko_wallet_ledger'
          AND column_name = 'balance_after'
          AND data_type <> 'numeric'
    ) THEN
        ALTER TABLE neko_wallet_ledger
        ALTER COLUMN balance_after TYPE NUMERIC(21, 2)
        USING balance_after::NUMERIC(21, 2);
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'neko_wallet_deletion_backups'
          AND column_name = 'original_balance'
          AND data_type <> 'numeric'
    ) THEN
        ALTER TABLE neko_wallet_deletion_backups
        ALTER COLUMN original_balance TYPE NUMERIC(21, 2)
        USING original_balance::NUMERIC(21, 2);
    END IF;
END $$;
ALTER TABLE users ALTER COLUMN neko_balance SET DEFAULT 0.00;

-- Persistent owner-grant confirmations for /sudo single, bulk, name, anime,
-- rarity-tier, and all-catalog selections. Keeping IDs server-side avoids unsafe/oversized
-- callback_data while a confirmation is waiting.
CREATE TABLE IF NOT EXISTS sudo_grants (
    id             BIGSERIAL PRIMARY KEY,
    chat_id        BIGINT NOT NULL,
    owner_id       BIGINT NOT NULL,
    target_user_id BIGINT NOT NULL,
    character_ids  BIGINT[] NOT NULL,
    selection_kind TEXT NOT NULL DEFAULT 'ids',
    -- FALSE is reserved for /sudo all -, which grants only missing catalog
    -- cards as visible-but-unranked inventory.
    is_ranked     BOOLEAN NOT NULL DEFAULT TRUE,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sudo_grants_pending_owner
ON sudo_grants (owner_id, status);
CREATE INDEX IF NOT EXISTS idx_sudo_grants_pending_target
ON sudo_grants (target_user_id, status);

-- Owner-confirmed broadcasts retain only the replied text/message reference.
-- Claiming a pending row atomically prevents a duplicate callback from sending
-- the same announcement to every registered group twice.
CREATE TABLE IF NOT EXISTS broadcast_jobs (
    id                BIGSERIAL PRIMARY KEY,
    owner_id          BIGINT NOT NULL,
    source_chat_id    BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    source_text       TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sending', 'completed', 'failed', 'cancelled')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at      TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ,
    delivered_count   INT NOT NULL DEFAULT 0,
    failed_count      INT NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_broadcast_jobs_pending_owner
ON broadcast_jobs (owner_id, status, created_at DESC);

-- Existing grants predate selection_kind; the default treats them as explicit
-- numeric grants. New name/anime/all grants retain their skip-owned mode.
ALTER TABLE sudo_grants ADD COLUMN IF NOT EXISTS selection_kind TEXT NOT NULL DEFAULT 'ids';
ALTER TABLE sudo_grants ADD COLUMN IF NOT EXISTS is_ranked BOOLEAN NOT NULL DEFAULT TRUE;

-- /deleteharem keeps a private, restorable snapshot rather than losing a
-- collection forever.  Each deletion has a header plus exact harem and
-- per-group-stat rows; /backharem restores the newest un-restored snapshot.
CREATE TABLE IF NOT EXISTS harem_deletion_backups (
    id                    BIGSERIAL PRIMARY KEY,
    user_id               BIGINT NOT NULL,
    deleted_by            BIGINT NOT NULL,
    original_total_claims INT NOT NULL DEFAULT 0,
    original_highest_rarity TEXT,
    record_count          INT NOT NULL DEFAULT 0,
    deleted_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_by           BIGINT,
    restored_at           TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_harem_backups_pending_user
ON harem_deletion_backups (user_id, deleted_at DESC)
WHERE restored_at IS NULL;

CREATE TABLE IF NOT EXISTS harem_deletion_backup_items (
    backup_id         BIGINT NOT NULL REFERENCES harem_deletion_backups (id) ON DELETE CASCADE,
    original_harem_id BIGINT NOT NULL,
    chat_id           BIGINT NOT NULL,
    character_id      BIGINT NOT NULL REFERENCES characters (id),
    is_favorite       BOOLEAN NOT NULL DEFAULT FALSE,
    is_ranked         BOOLEAN NOT NULL DEFAULT TRUE,
    acquired_at       TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (backup_id, original_harem_id)
);
CREATE INDEX IF NOT EXISTS idx_harem_backup_items_character
ON harem_deletion_backup_items (character_id);
ALTER TABLE harem_deletion_backup_items
ADD COLUMN IF NOT EXISTS is_ranked BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS harem_deletion_backup_group_stats (
    backup_id    BIGINT NOT NULL REFERENCES harem_deletion_backups (id) ON DELETE CASCADE,
    chat_id      BIGINT NOT NULL,
    claims       INT NOT NULL DEFAULT 0,
    rarity_score INT NOT NULL DEFAULT 0,
    PRIMARY KEY (backup_id, chat_id)
);

CREATE TABLE IF NOT EXISTS group_stats (
    chat_id       BIGINT PRIMARY KEY REFERENCES groups (chat_id) ON DELETE CASCADE,
    total_spawns  INT NOT NULL DEFAULT 0,
    total_claims  INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_group_stats (
    user_id      BIGINT NOT NULL,
    chat_id      BIGINT NOT NULL,
    claims       INT NOT NULL DEFAULT 0,
    rarity_score INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, chat_id)
);

-- The Mini App market supports both Full Owner catalog-stock listings and
-- player-to-player listings. Player cards are moved out of Harem into this
-- escrow record at listing time, so one physical copy can never be burned,
-- gifted, traded, or sold twice while it is listed.
CREATE TABLE IF NOT EXISTS market_listings (
    id                    BIGSERIAL PRIMARY KEY,
    character_id          BIGINT NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
    price                 NUMERIC(21, 2) NOT NULL CHECK (price > 0),
    -- The actor who created either kind of listing; retained for audit/UI use.
    listed_by             BIGINT NOT NULL,
    listing_kind          TEXT NOT NULL DEFAULT 'catalog'
                            CHECK (listing_kind IN ('catalog', 'player')),
    -- Only player listings have a seller and an escrowed former Harem copy.
    seller_user_id        BIGINT REFERENCES users (user_id) ON DELETE CASCADE,
    source_harem_id       BIGINT,
    source_chat_id        BIGINT,
    source_is_favorite    BOOLEAN NOT NULL DEFAULT FALSE,
    source_is_ranked      BOOLEAN NOT NULL DEFAULT TRUE,
    source_acquired_at    TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_listings_updated
ON market_listings (updated_at DESC, id DESC);

-- v8 market escrow migration. Pre-v8 rows are official catalog-stock listings.
-- A catalog listing remains one copy per character, while many members may
-- safely list distinct Harem copies of the same catalog card.
ALTER TABLE market_listings DROP CONSTRAINT IF EXISTS market_listings_character_id_key;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS listing_kind TEXT NOT NULL DEFAULT 'catalog';
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS seller_user_id BIGINT REFERENCES users (user_id) ON DELETE CASCADE;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS source_harem_id BIGINT;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS source_chat_id BIGINT;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS source_is_favorite BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS source_is_ranked BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE market_listings ADD COLUMN IF NOT EXISTS source_acquired_at TIMESTAMPTZ;
UPDATE market_listings SET listing_kind = 'catalog' WHERE listing_kind IS NULL;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'market_listings_listing_kind_check'
          AND conrelid = 'market_listings'::regclass
    ) THEN
        ALTER TABLE market_listings
        ADD CONSTRAINT market_listings_listing_kind_check
        CHECK (listing_kind IN ('catalog', 'player'));
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_listings_one_catalog_character
ON market_listings (character_id)
WHERE listing_kind = 'catalog';
CREATE UNIQUE INDEX IF NOT EXISTS idx_market_listings_one_source_harem
ON market_listings (source_harem_id)
WHERE source_harem_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_market_listings_player_seller
ON market_listings (seller_user_id, updated_at DESC)
WHERE listing_kind = 'player';

-- Eternal and OverLord cards are permanently non-transferable. A historical player
-- escrow must be returned as a fresh Harem row before its stale listing is
-- removed; deleting escrow alone would silently destroy an owned card.  The
-- Favorite flag is restored only if it is still globally available.
WITH stale_nontransferable_escrows AS (
    SELECT
        listing.*,
        ROW_NUMBER() OVER (
            PARTITION BY listing.seller_user_id
            ORDER BY CASE WHEN COALESCE(listing.source_is_favorite, FALSE)
                          THEN 0 ELSE 1 END,
                     listing.id ASC
        ) AS favorite_restore_order
    FROM market_listings AS listing
    JOIN characters AS character ON character.id = listing.character_id
    WHERE listing.listing_kind = 'player'
      AND listing.seller_user_id IS NOT NULL
      AND LOWER(BTRIM(COALESCE(character.rarity, ''))) IN ('eternal', 'overlord')
)
INSERT INTO harem (
    user_id, chat_id, character_id, is_favorite, is_ranked, acquired_at
)
SELECT
    listing.seller_user_id,
    COALESCE(listing.source_chat_id, 0),
    listing.character_id,
    CASE
        WHEN COALESCE(listing.source_is_favorite, FALSE) = TRUE
         AND listing.favorite_restore_order = 1
         AND NOT EXISTS (
             SELECT 1 FROM harem AS current_favorite
             WHERE current_favorite.user_id = listing.seller_user_id
               AND current_favorite.is_favorite = TRUE
         )
        THEN TRUE
        ELSE FALSE
    END,
    COALESCE(listing.source_is_ranked, TRUE),
    COALESCE(listing.source_acquired_at, NOW())
FROM stale_nontransferable_escrows AS listing;

-- Listing a ranked player card removes it from the seller's global collection
-- totals (but not historic group stats). Rebuild those global values for every
-- seller whose stale protected escrow was restored above.
UPDATE users AS restored_user
SET
    total_claims = COALESCE((
        SELECT COUNT(*)
        FROM harem AS restored_harem
        WHERE restored_harem.user_id = restored_user.user_id
          AND restored_harem.is_ranked = TRUE
    ), 0),
    highest_rarity = (
        SELECT restored_character.rarity
        FROM harem AS restored_harem
        JOIN characters AS restored_character
          ON restored_character.id = restored_harem.character_id
        WHERE restored_harem.user_id = restored_user.user_id
          AND restored_harem.is_ranked = TRUE
        ORDER BY restored_character.rarity_score DESC,
                 restored_character.id ASC,
                 restored_harem.id ASC
        LIMIT 1
    )
WHERE restored_user.user_id IN (
    SELECT DISTINCT listing.seller_user_id
    FROM market_listings AS listing
    JOIN characters AS character ON character.id = listing.character_id
    WHERE listing.listing_kind = 'player'
      AND listing.seller_user_id IS NOT NULL
      AND LOWER(BTRIM(COALESCE(character.rarity, ''))) IN ('eternal', 'overlord')
);

-- Official stock has no user inventory to return.  After player escrows are
-- restored, remove every stale Eternal/OverLord listing of either kind.
DELETE FROM market_listings AS listing
USING characters AS character
WHERE character.id = listing.character_id
  AND LOWER(BTRIM(COALESCE(character.rarity, ''))) IN ('eternal', 'overlord');
"""