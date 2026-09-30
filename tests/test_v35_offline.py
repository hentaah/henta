"""Offline, dependency-stubbed regression tests for the v35 controls.

These tests intentionally do not import a real aiogram/asyncpg/Redis client and
never open a network connection.  They exercise the new policy parsers and the
transactional rarity service with small in-memory stand-ins instead.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MISSING = object()


@contextlib.contextmanager
def patched_modules(mapping: dict[str, types.ModuleType]):
    """Temporarily expose dependency stubs under their production import names."""
    old = {name: sys.modules.get(name, MISSING) for name in mapping}
    sys.modules.update(mapping)
    try:
        yield
    finally:
        for name, previous in old.items():
            if previous is MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def load_source_module(name: str, relative_path: str):
    """Load a source file under an isolated test-only module name."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeMessage:
    def __init__(self, text: str, *, chat_type: str = "supergroup", user_id: int = 7):
        self.text = text
        self.chat = types.SimpleNamespace(type=chat_type)
        self.from_user = types.SimpleNamespace(id=user_id, is_bot=False)
        self.via_bot = None
        self.replies: list[tuple[tuple, dict]] = []
        self.answers: list[tuple[tuple, dict]] = []

    async def reply(self, *args, **kwargs):
        self.replies.append((args, kwargs))

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class FakeTelegramObject:
    pass


def aiogram_stubs(*, include_filter: bool = False) -> dict[str, types.ModuleType]:
    """Build the minimum aiogram surface needed by middleware/helper imports."""
    aiogram = types.ModuleType("aiogram")
    aiogram.BaseMiddleware = type("BaseMiddleware", (), {})
    aiogram.Bot = type("Bot", (), {})

    if include_filter:
        class TextMagic:
            @staticmethod
            def regexp(pattern: str):
                return re.compile(pattern)

        aiogram.F = types.SimpleNamespace(text=TextMagic())

    types_module = types.ModuleType("aiogram.types")
    types_module.Message = FakeMessage
    types_module.TelegramObject = FakeTelegramObject
    types_module.CallbackQuery = type("CallbackQuery", (), {})
    types_module.InlineQuery = type("InlineQuery", (), {})

    class InlineKeyboardButton:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class InlineKeyboardMarkup:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    types_module.InlineKeyboardButton = InlineKeyboardButton
    types_module.InlineKeyboardMarkup = InlineKeyboardMarkup
    return {"aiogram": aiogram, "aiogram.types": types_module}


def basic_service_stubs() -> dict[str, types.ModuleType]:
    pool_module = types.ModuleType("bot.db.pool")
    pool_module.get_pool = lambda: None
    redis_module = types.ModuleType("bot.services.redis_client")
    redis_module.get_redis = lambda: None
    return {"bot.db.pool": pool_module, "bot.services.redis_client": redis_module}


class V35CommandRecognitionTests(unittest.TestCase):
    def _load_moderation(self):
        stubs = aiogram_stubs()
        stubs.update(basic_service_stubs())
        with patched_modules(stubs):
            return load_source_module("v35_moderation_test", "bot/services/moderation.py")

    def test_dot_aliases_are_exact_commands_not_ordinary_chat(self):
        moderation = self._load_moderation()
        self.assertEqual(moderation.dot_player_command_name(" .Casino  "), "casino")
        self.assertEqual(moderation.dot_player_command_name(".henmode"), "henmode")
        self.assertIsNone(moderation.dot_player_command_name(".casinobox"))
        self.assertIsNone(moderation.dot_player_command_name("hello .casino"))

        # Owner controls returned to slash syntax; !text is ordinary chat and
        # must not be treated as an owner command or excluded from reply input.
        self.assertTrue(moderation.is_ordinary_group_message(FakeMessage("!stoprarityspawn Mythical")))
        self.assertTrue(moderation.is_ordinary_group_message(FakeMessage(" !spawnstart\tRare")))

        self.assertFalse(moderation.is_ordinary_group_message(FakeMessage(".casino")))
        self.assertFalse(moderation.is_ordinary_group_message(FakeMessage(".gift 42")))
        self.assertTrue(moderation.is_ordinary_group_message(FakeMessage(".unknown command")))

    def test_dot_filter_accepts_only_the_requested_alias_boundary(self):
        stubs = aiogram_stubs(include_filter=True)
        with patched_modules(stubs):
            common = load_source_module("v35_common_test", "bot/handlers/common.py")

        casino = common.dot_player_command_filter("casino", accepts_arguments=True)
        self.assertIsNotNone(casino.fullmatch(".casino"))
        self.assertIsNotNone(casino.fullmatch("  .CASINO 50"))
        self.assertIsNone(casino.fullmatch(".casinobox"))

        harem = common.dot_player_command_filter("harem")
        self.assertIsNotNone(harem.fullmatch(".harem  "))
        self.assertIsNone(harem.fullmatch(".harem page2"))

        # The broad Casino stake-reply handler appears before later command
        # routers. It must reject slash and requested dot commands before an
        # inner throttling middleware gets an opportunity to count them.
        reply_input = common.non_command_reply_text_filter()
        self.assertIsNotNone(reply_input.fullmatch("50.00"))
        self.assertIsNotNone(reply_input.fullmatch("  not a command"))
        for command_text in ("/casino", "  /casino", ".gift 44", "  .trade 1 2"):
            self.assertIsNone(reply_input.fullmatch(command_text), command_text)
        for ordinary_text in ("!broadcast", "  !broadcast"):
            self.assertIsNotNone(reply_input.fullmatch(ordinary_text), ordinary_text)

    def test_required_membership_gate_recognizes_protected_dot_aliases(self):
        moderation = self._load_moderation()
        stubs = aiogram_stubs()
        stubs.update({"bot.services.moderation": moderation})
        with patched_modules(stubs):
            membership = load_source_module("v35_membership_test", "bot/middlewares/membership.py")

        self.assertEqual(membership.protected_command_name(FakeMessage(".casino")), "casino")
        self.assertEqual(membership.protected_command_name(FakeMessage(".harem")), "harem")
        self.assertEqual(membership.protected_command_name(FakeMessage(".gift 10")), "gift")
        self.assertIsNone(membership.protected_command_name(FakeMessage(".trade 1 2")))
        self.assertIsNone(membership.protected_command_name(FakeMessage(".henmode")))

        async def no_delay(_seconds: float):
            return None

        membership.asyncio.sleep = no_delay
        middleware = membership.RequiredChannelMembershipMiddleware()
        invoked: list[bool] = []

        async def handler(event, data):
            invoked.append(True)
            return "handled"

        class LeftChannelBot:
            async def get_chat_member(self, *, chat_id, user_id):
                return types.SimpleNamespace(status="left", is_member=False)

        blocked = FakeMessage(".casino")
        result = asyncio.run(middleware(handler, blocked, {"bot": LeftChannelBot()}))
        self.assertIsNone(result)
        self.assertEqual(invoked, [])
        self.assertEqual(len(blocked.replies), 1)
        self.assertIn("/casino", blocked.replies[0][0][0])

        class MemberBot:
            async def get_chat_member(self, *, chat_id, user_id):
                return types.SimpleNamespace(status="member", is_member=True)

        allowed = FakeMessage(".casino")
        result = asyncio.run(middleware(handler, allowed, {"bot": MemberBot()}))
        self.assertEqual(result, "handled")
        self.assertEqual(invoked, [True])

    def test_middlewares_recognize_dot_aliases_and_new_pause_controls(self):
        moderation = self._load_moderation()
        stubs = aiogram_stubs()
        stubs.update(basic_service_stubs())
        stubs["bot.services.moderation"] = moderation
        config = types.ModuleType("bot.config")
        config.default_spawn_interval_for_group = lambda _chat_id: 125
        stubs["bot.config"] = config
        spawn_engine = types.ModuleType("bot.services.spawn_engine")
        spawn_engine.maybe_spawn = lambda *args, **kwargs: None
        stubs["bot.services.spawn_engine"] = spawn_engine
        with patched_modules(stubs):
            activity = load_source_module("v35_activity_test", "bot/middlewares/activity.py")
            throttling = load_source_module("v35_throttling_test", "bot/middlewares/throttling.py")
            response_delay = load_source_module("v35_response_delay_test", "bot/middlewares/response_delay.py")

        self.assertTrue(activity._is_spawn_control_command(FakeMessage("/stoprarityspawn OP")))
        self.assertTrue(activity._is_spawn_control_command(FakeMessage("/spawnstart@HentaCatcherBot OP")))
        self.assertFalse(activity._is_spawn_control_command(FakeMessage("/spawnstarter OP")))
        self.assertFalse(activity._is_spawn_control_command(FakeMessage("!spawnstart OP")))
        self.assertEqual(throttling.ThrottlingMiddleware._extract_command(FakeMessage(".gift 44")), "gift")
        self.assertEqual(response_delay.CommandResponseDelayMiddleware._command_name(FakeMessage(".henmode")), "henmode")


class FakeBanConnection:
    """Small status-query stand-in for permanent-Boom middleware tests."""

    def __init__(self, *, is_banned: bool = True, reason: str = "spam", actor: str = "Owner"):
        self.status = {
            "is_banned": is_banned,
            "boom_reason": reason,
            "boomed_by_name": actor,
            "spam_remaining": 0,
            "manual_mute_remaining": 0,
        }
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        return self.status

    async def fetchval(self, query: str, *args):
        self.calls.append((query, args))
        return None

    async def execute(self, query: str, *args):
        self.calls.append((query, args))
        return "UPDATE 1"


class V35BoomAccessTests(unittest.TestCase):
    def _load_ban(self, connection: FakeBanConnection):
        stubs = aiogram_stubs()
        stubs.update(basic_service_stubs())
        config = types.ModuleType("bot.config")
        config.settings = types.SimpleNamespace(owner_id=999_999)
        stubs["bot.config"] = config
        moderation = self._load_moderation()
        stubs["bot.services.moderation"] = moderation
        stubs["bot.db.pool"].get_pool = lambda: FakePool(connection)
        with patched_modules(stubs):
            return load_source_module("v35_ban_test", "bot/middlewares/ban.py")

    def _load_moderation(self):
        stubs = aiogram_stubs()
        stubs.update(basic_service_stubs())
        with patched_modules(stubs):
            return load_source_module("v35_boom_moderation_test", "bot/services/moderation.py")

    def test_boom_notice_uses_stored_reason_and_actor_with_html_escaping(self):
        ban = self._load_ban(FakeBanConnection())
        notice = ban._boom_notice({
            "boom_reason": "selling <auto> & spam",
            "boomed_by_name": "Rose & Co",
        })
        self.assertEqual(
            notice,
            "🚫 BOOM!\n"
            "📝 Reason: selling &lt;auto&gt; &amp; spam\n"
            "👮Boomed by: Rose &amp; Co",
        )

    def test_boomed_member_gets_notice_for_other_commands_but_profile_is_allowed(self):
        connection = FakeBanConnection(reason="Selling Auto catch", actor="═薔 Роза 薇═")
        ban = self._load_ban(connection)
        middleware = ban.BanMiddleware()

        called: list[dict] = []

        async def handler(_event, data):
            called.append(dict(data))
            return "handled"

        blocked = FakeMessage("/harem")
        result = asyncio.run(middleware(handler, blocked, {}))
        self.assertIsNone(result)
        self.assertEqual(called, [])
        self.assertEqual(len(blocked.answers), 1)
        self.assertEqual(
            blocked.answers[0][0][0],
            "🚫 BOOM!\n📝 Reason: Selling Auto catch\n👮Boomed by: ═薔 Роза 薇═",
        )
        self.assertEqual(blocked.answers[0][1]["parse_mode"], "HTML")

        allowed = FakeMessage("/profile@HentaCatcherBot")
        data: dict = {}
        result = asyncio.run(middleware(handler, allowed, data))
        self.assertEqual(result, "handled")
        self.assertTrue(data["boom_ban_active"])
        self.assertEqual(len(allowed.answers), 0)

    def test_boom_command_detection_covers_slash_and_requested_dot_aliases_only(self):
        ban = self._load_ban(FakeBanConnection())
        self.assertTrue(ban._is_boom_blocked_command(FakeMessage("/catch")))
        self.assertTrue(ban._is_boom_blocked_command(FakeMessage("  .gift 11")))
        self.assertFalse(ban._is_boom_blocked_command(FakeMessage("!broadcast")))
        self.assertFalse(ban._is_boom_blocked_command(FakeMessage("ordinary chat")))

    def test_boom_profile_flag_skips_activity_side_effects(self):
        moderation = self._load_moderation()
        stubs = aiogram_stubs()
        stubs.update(basic_service_stubs())
        stubs["bot.services.moderation"] = moderation
        config = types.ModuleType("bot.config")
        config.default_spawn_interval_for_group = lambda _chat_id: 125
        stubs["bot.config"] = config
        spawn_engine = types.ModuleType("bot.services.spawn_engine")
        spawn_engine.maybe_spawn = lambda *args, **kwargs: None
        stubs["bot.services.spawn_engine"] = spawn_engine
        with patched_modules(stubs):
            activity = load_source_module("v35_boom_activity_test", "bot/middlewares/activity.py")

        invoked: list[bool] = []

        async def handler(_event, _data):
            invoked.append(True)
            return "profile"

        result = asyncio.run(
            activity.ActivityMiddleware()(handler, FakeMessage("/profile"), {"boom_ban_active": True})
        )
        self.assertEqual(result, "profile")
        self.assertEqual(invoked, [True])

    def test_boom_reason_normalization_and_persistence_contract_are_present(self):
        moderation = self._load_moderation()
        self.assertEqual(moderation.normalize_boom_reason("  selling\n  auto   catch  "), "selling auto catch")
        self.assertIsNone(moderation.normalize_boom_reason("   "))
        self.assertIsNone(moderation.normalize_boom_reason("x" * (moderation.MAX_BOOM_REASON_LENGTH + 1)))

        schema = (ROOT / "bot/db/schema.py").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS boom_bans", schema)
        self.assertIn("boomed_by_name", schema)
        admin = (ROOT / "bot/handlers/admin.py").read_text()
        self.assertIn('Command("boomlist")', admin)
        self.assertIn("BoomReason.waiting_reason", admin)
        self.assertIn("apply_boom_ban", admin)
        self.assertIn("DELETE FROM boom_bans WHERE user_id = $1", admin)


class FakeRarityConnection:
    def __init__(self, *, label: str | None = "Legendary", previous: bool | None = None, deleted: int = 0):
        self.label = label
        self.previous = previous
        self.deleted = deleted
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self):
        return AsyncContext(self)

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        if "SELECT BTRIM(rarity) AS label" in query:
            return None if self.label is None else {"label": self.label, "rarity_key": self.label.lower()}
        if "SELECT spawn_enabled" in query and "catalog_rarity_spawn_controls" in query:
            return None if self.previous is None else {"spawn_enabled": self.previous}
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query: str, *args):
        self.calls.append((query, args))

    async def fetch(self, query: str, *args):
        self.calls.append((query, args))
        if "DELETE FROM scheduled_op_spawns" in query:
            return [{"chat_id": index} for index in range(self.deleted)]
        raise AssertionError(f"Unexpected fetch query: {query}")


class FakePool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return AsyncContext(self.connection)


def game_service_stubs(pool: FakePool) -> dict[str, types.ModuleType]:
    pool_module = types.ModuleType("bot.db.pool")
    pool_module.get_pool = lambda: pool

    neko = types.ModuleType("bot.services.neko")
    neko.MAX_NEKO_BALANCE = 0
    neko.NEKO_QUANTUM = 0
    neko._record_wallet_ledger = lambda *args, **kwargs: None

    love = types.ModuleType("bot.services.love")
    love.LOVE_LEVEL_TARGETS = {}
    love.LOVE_RARITY = "Love"
    love.grant_love_state_in_transaction = lambda *args, **kwargs: None

    ranks = types.ModuleType("bot.services.special_ranks")
    ranks.ETERNAL_RARITY = "Eternal"
    ranks.OVERLORD_RARITY = "OverLord"
    ranks.OVERLORD_UNIQUE_OWNER_CAP = 10
    ranks.SUSPICIOUS_RARITY = "Suspicious"
    ranks.is_overlord = lambda value: str(value).casefold() == "overlord"
    return {
        "bot.db.pool": pool_module,
        "bot.services.neko": neko,
        "bot.services.love": love,
        "bot.services.special_ranks": ranks,
    }


class V35RarityServiceTests(unittest.TestCase):
    def _load_game(self, connection: FakeRarityConnection):
        stubs = game_service_stubs(FakePool(connection))
        with patched_modules(stubs):
            return load_source_module("v35_game_test", "bot/services/game.py")

    def test_stop_rarity_persists_a_canonical_global_control_and_cancels_queued_op(self):
        connection = FakeRarityConnection(label="Legendary", previous=True, deleted=2)
        game = self._load_game(connection)
        result = asyncio.run(game.set_rarity_spawn_enabled("  legendary ", enabled=False))

        self.assertEqual(result["label"], "Legendary")
        self.assertEqual(result["rarity_key"], "legendary")
        self.assertFalse(result["spawn_enabled"])
        self.assertTrue(result["was_spawn_enabled"])
        self.assertEqual(result["cancelled_scheduled_op_count"], 2)

        queries = "\n".join(query for query, _args in connection.calls)
        self.assertIn("pg_advisory_xact_lock", queries)
        self.assertIn("INSERT INTO catalog_rarity_spawn_controls", queries)
        self.assertIn("ON CONFLICT (rarity_key)", queries)
        self.assertIn("DELETE FROM scheduled_op_spawns", queries)
        advisory_call = next(args for query, args in connection.calls if "pg_advisory_xact_lock" in query)
        self.assertEqual(advisory_call, ("rarity-spawn:legendary",))

    def test_restart_is_an_upsert_and_love_never_claims_to_be_spawnable(self):
        connection = FakeRarityConnection(label="Rare", previous=False)
        game = self._load_game(connection)
        result = asyncio.run(game.set_rarity_spawn_enabled("rare", enabled=True))
        self.assertFalse(result["was_spawn_enabled"])
        self.assertTrue(result["spawn_enabled"])
        self.assertEqual(result["cancelled_scheduled_op_count"], 0)

        love_connection = FakeRarityConnection(label="Love")
        love_game = self._load_game(love_connection)
        love = asyncio.run(love_game.set_rarity_spawn_enabled("Love", enabled=False))
        self.assertEqual(love, {"reason": "love_not_spawnable", "label": "Love"})
        self.assertFalse(any("pg_advisory_xact_lock" in query for query, _ in love_connection.calls))


class FakeSpawnConnection:
    def __init__(self, *, rarity_enabled: bool | None, event_enabled: bool | None, missing: bool = False):
        self.rarity_enabled = rarity_enabled
        self.event_enabled = event_enabled
        self.missing = missing
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query: str, *args):
        self.calls.append((query, args))
        if "FROM characters" in query:
            return None if self.missing else {"rarity_label": "Mythical", "rarity_key": "mythical", "event_label": "Bunny"}
        if "catalog_rarity_spawn_controls" in query:
            return None if self.rarity_enabled is None else {"spawn_enabled": self.rarity_enabled}
        if "FROM catalog_events" in query:
            return None if self.event_enabled is None else {"spawn_enabled": self.event_enabled}
        raise AssertionError(f"Unexpected spawn fetchrow query: {query}")

    async def execute(self, query: str, *args):
        self.calls.append((query, args))


def spawn_engine_stubs() -> dict[str, types.ModuleType]:
    stubs = aiogram_stubs()
    config = types.ModuleType("bot.config")
    config.HENTACATCHER_GP_CHAT_ID = -100
    config.default_spawn_interval_for_group = lambda _chat_id: 125
    config.settings = types.SimpleNamespace()

    pool = types.ModuleType("bot.db.pool")
    pool.get_pool = lambda: None
    redis = types.ModuleType("bot.services.redis_client")
    redis.get_redis = lambda: None
    rich = types.ModuleType("bot.services.rich_renderer")
    rich.rarity_symbol = lambda _rarity: "★"
    ranks = types.ModuleType("bot.services.special_ranks")
    ranks.MAIN_SUSPICIOUS_SPAWNS_REQUIRED = 40
    ranks.OVERLORD_COUNTDOWN_REMAINING = 5
    ranks.OVERLORD_LOWER_SPAWNS_REQUIRED = 70
    ranks.OVERLORD_NOTICE_AT_LOWER_SPAWNS = 65
    ranks.OVERLORD_RARITY = "OverLord"
    ranks.OVERLORD_UNIQUE_OWNER_CAP = 10
    ranks.SUSPICIOUS_EXTERNAL_LOWER_SPAWNS_REQUIRED = 70
    ranks.SUSPICIOUS_MAIN_GROUP_WEIGHT = 0
    ranks.SUSPICIOUS_RARITY = "Suspicious"
    ranks.is_love = lambda value: str(value).strip().casefold() == "love"
    ranks.is_overlord = lambda value: str(value).strip().casefold() == "overlord"
    ranks.is_suspicious = lambda value: str(value).strip().casefold() == "suspicious"
    stubs.update({
        "bot.config": config,
        "bot.db.pool": pool,
        "bot.services.redis_client": redis,
        "bot.services.rich_renderer": rich,
        "bot.services.special_ranks": ranks,
    })
    return stubs


class V35SpawnPolicyTests(unittest.TestCase):
    def _load_engine(self):
        with patched_modules(spawn_engine_stubs()):
            return load_source_module("v35_spawn_engine_test", "bot/services/spawn_engine.py")

    def test_live_policy_reports_rarity_and_event_separately_with_locking(self):
        engine = self._load_engine()
        rarity_connection = FakeSpawnConnection(rarity_enabled=False, event_enabled=True)
        reason = asyncio.run(
            engine._character_spawn_block_reason(91, conn=rarity_connection, lock_controls=True)
        )
        self.assertEqual(reason, "rarity")
        rarity_queries = "\n".join(query for query, _args in rarity_connection.calls)
        self.assertIn("pg_advisory_xact_lock", rarity_queries)
        self.assertIn("catalog_rarity_spawn_controls", rarity_queries)
        self.assertIn("FOR UPDATE", rarity_queries)

        event_connection = FakeSpawnConnection(rarity_enabled=True, event_enabled=False)
        reason = asyncio.run(engine._character_spawn_block_reason(92, conn=event_connection))
        self.assertEqual(reason, "event")

        absent_connection = FakeSpawnConnection(rarity_enabled=None, event_enabled=None, missing=True)
        reason = asyncio.run(engine._character_spawn_block_reason(93, conn=absent_connection))
        self.assertEqual(reason, "not_found")

    def test_source_registers_each_requested_alias_and_rarity_control(self):
        expected_decorators = {
            "bot/handlers/neko.py": 'dot_player_command_filter("casino", accepts_arguments=True)',
            "bot/handlers/harem.py": 'dot_player_command_filter("harem")',
            "bot/handlers/trade.py": 'dot_player_command_filter("trade", accepts_arguments=True)',
            "bot/handlers/henmode.py": 'dot_player_command_filter("henmode")',
            "bot/handlers/fav_gift.py": 'dot_player_command_filter("gift", accepts_arguments=True)',
        }
        for relative_path, marker in expected_decorators.items():
            self.assertIn(marker, (ROOT / relative_path).read_text(), relative_path)

        neko = (ROOT / "bot/handlers/neko.py").read_text()
        self.assertIn("non_command_reply_text_filter", neko)
        self.assertIn(
            "@router.message(F.reply_to_message, F.text, non_command_reply_text_filter())",
            neko,
        )

        owner_tools = (ROOT / "bot/handlers/owner_tools.py").read_text()
        for command in ("getgroups", "left", "broadcast", "stopspawn", "restorespawn", "stoprarityspawn", "spawnstart", "kill", "unkill"):
            self.assertIn(f'Command("{command}")', owner_tools)
        self.assertNotIn("_bang_command_filter", owner_tools)
        self.assertIn("set_rarity_spawn_enabled", owner_tools)
        self.assertIn("not actor or not await is_bot_owner(actor.id)", owner_tools)

        admin = (ROOT / "bot/handlers/admin.py").read_text()
        self.assertIn("rarity_spawn_stopped", admin)
        self.assertIn("/stoprarityspawn &lt;rarity&gt;", admin)
        self.assertIn("/spawnstart &lt;rarity&gt;", admin)
        self.assertIn("/boomlist", admin)
        self.assertIn(" — pause rarity", admin)
        guide_block = admin[admin.index("OWNER_GUIDES = {"):admin.index("async def _edit_owner_guide")]
        guide_lines = [line for line in guide_block.splitlines() if "<code>" in line]
        self.assertGreater(len(guide_lines), 20)
        self.assertTrue(all(" — " in line for line in guide_lines))

        schema = (ROOT / "bot/db/schema.py").read_text()
        self.assertIn("CREATE TABLE IF NOT EXISTS catalog_rarity_spawn_controls", schema)
        self.assertIn("rarity_key     TEXT PRIMARY KEY", schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
