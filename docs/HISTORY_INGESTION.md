# History Ingestion

Status: **Approved baseline v1**. Goal: store **every accessible message** of an activated group, including history from before the bot joined, and always know exactly what is and isn't stored.

## 1. Telegram constraints that drive the design

| Constraint | Consequence |
|---|---|
| Bot API bots **cannot read chat history**. There is no history method; bots only receive updates sent after they are in the chat | Live collection alone can never cover pre-deployment history → Paths B and C |
| In privacy mode (the default), bots only see commands, replies to them, and mentions. "Bot admins and bots with privacy mode disabled will receive all messages except messages sent by other bots" (Bot FAQ) | Setup requires `/setprivacy → Disable` in BotFather **before** adding the bot (or re-adding it), or making the bot an admin. Activation verifies this (§3.4) |
| Bots never see other bots' messages | Coverage reports state "messages from other bots are not collected via Bot API". Paths B and C do include them |
| Unfetched updates are kept server-side for at most 24 h | A bot outage longer than 24 h causes permanent Bot API loss → recorded as a suspected gap; fill it from Path B or C |
| No deletion updates for bots in groups | Deletions are only detected by Path C (MTProto) or admin action |
| Supergroup message IDs are per-chat and identical across Bot API, MTProto, and Desktop export | `(chat_id, tg_message_id)` is the natural key for supergroups |
| Basic-group message IDs are per-user | Cross-source dedup needs fingerprints; recommend converting to a supergroup |
| Basic group → supergroup migration changes the chat ID and restarts message IDs | Record `migrated_from_tg_chat_id`; keep both chats linked; the UI shows them as one timeline with a separator |
| Desktop export `id` for a supergroup is the bare channel ID (no `-100` prefix); `from_id` is `"user123"` / `"channel456"` | Normalizer maps `id → -100<id>` for supergroups/channels and parses `from_id` prefixes |
| Desktop export `date` is in the **exporter's local time**; `date_unixtime` is UTC epoch | Always use `date_unixtime`. Fall back to `date` + an explicit tz prompt only if it is missing (very old exports) |
| Telegram API terms: AI use of platform data needs explicit per-user consent (SECURITY.md §3) | Ingestion stores data. The **AI layer**, not ingestion, enforces consent |

## 2. One normalized shape

Every path produces `NormalizedMessage` (pydantic, in `ingestion/normalizer.py`):

```python
class NormalizedMessage(BaseModel):
    source: Literal["bot_api", "desktop_export", "mtproto"]
    tg_chat_id: int                 # Bot API form
    tg_message_id: int
    message_thread_id: int | None
    sender: SenderRef | None        # user (tg_user_id, username, names, is_bot) or sender_chat
    sent_at: AwareDatetime          # UTC
    edited_at: AwareDatetime | None
    text: str                       # plain text (caption for media); "" if none
    entities: list[Entity]          # normalized (type, offset, length, url)
    reply_to_tg_message_id: int | None
    forward_origin: ForwardOrigin | None
    media_type: str | None
    media_meta: dict | None
    is_service: bool
    service_action: str | None
    raw_ref: str | None             # e.g. "export:result.json#messages[1234]" for error reports
```

`MessageStore.upsert(batch: list[NormalizedMessage], run: ImportRun | None) -> UpsertStats` is the **only** writer to `messages`.

## 3. Path A — Live Bot API collection (Phase 2)

### 3.1 Handlers
- `MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND …)` for `message`, plus `edited_message` (with `channel_post`/`edited_channel_post` only if a channel is ever activated).
- Commands addressed to the bot are both persisted (as messages) and routed to the UI. Bot replies sent by us are persisted with `sender.is_bot=True` and a `bot_self` marker so they can be excluded from analysis.
- `my_chat_member` updates track the bot being added, removed, promoted, or restricted, and update chat status.
- `chat_member` updates (admin-only) maintain `chat_members`. They are optional and require `allowed_updates` to include them.
- Run with `allowed_updates=[message, edited_message, my_chat_member, chat_member, callback_query]` and `drop_pending_updates=False`, so updates queued during a restart are not lost.

### 3.2 Upsert semantics
1. Normalize. Messages from unactivated chats are **dropped and not stored**: the bot stores nothing for a chat until an owner activates it.
2. `INSERT … ON CONFLICT (chat_id, tg_message_id) DO UPDATE` only when incoming data is newer (`edited_at` greater), or when it fills a null field such as `message_thread_id` from a richer source.
3. If text or entities change, append `message_versions`.
4. Upsert `message_sources(message_id, 'bot_api')`.
5. Set `index_state='pending'`. An in-process indexer task drains pending rows in batches of 200 (deterministic extraction + FTS is trigger-maintained).
6. Update `ingestion_state(chat,'bot_api')` and extend the current `observed` coverage segment.

Duplicate updates (PTB redelivery) are naturally idempotent.

### 3.3 Restart and outage handling
- At startup, read `ingestion_state.last_seen_at`. If `now - last_seen_at > 24h` (or the process heartbeat shows a > 24 h gap), insert a `gap_suspected` segment `[last_seen_at, first update after restart]` and alert owners in DM with fix options (import an export covering the window, or run MTProto backfill).
- Network errors are handled by PTB's retry with backoff. Each disconnect > 5 min is logged as a `polling_outage` event, but only outages > 24 h create gaps.

### 3.4 Activation checks
`/activate` (owner only, issued in the group):
1. `getChatMember(bot)`: is it admin? Otherwise call `getMe().can_read_all_group_messages`. If neither, refuse with the BotFather instructions.
2. Record the chat (`status=active`), post the participant notice (SECURITY.md §3), and DM the owner a setup checklist (timezone, import history, schedules).

## 4. Path B — Telegram Desktop JSON export import (Phase 2) — **primary history path**

### 4.1 Operator steps (document in-bot under Help → "Import history")
Telegram Desktop → open the group → ⋮ → **Export chat history** → uncheck all media → Format: **Machine-readable JSON** → date range "from the oldest" → Export. This produces `result.json`.

### 4.2 Delivery to the bot
- **Small files (≤ 20 MB, the Bot API download limit):** send `result.json` (or `.zip` containing it) as a document to the bot in DM → the bot asks which activated chat it belongs to (or auto-matches `id`) → import job.
- **Large files:** `scp` to the VPS `/data/imports/` and run `elysium-bot import-export /data/imports/result.json --chat <tg_chat_id>`. A local Bot API server would raise the limit, but it adds a service and is out of scope.

### 4.3 Parser
- Stream with `ijson` over `messages.item`, so memory use is constant. Top-level `id`/`type`/`name` are read first to validate the target chat. Mismatches abort unless `--force-chat`.
- Full-account exports (`chats.list[]`) are supported by selecting the chat with a matching ID.
- Field mapping:

| Export field | Normalized |
|---|---|
| `id` | `tg_message_id` |
| `type` = `message` / `service` | `is_service` |
| `date_unixtime` (fallback `date`) | `sent_at` |
| `edited_unixtime` (fallback `edited`) | `edited_at` |
| `from`, `from_id` (`user<N>` / `channel<N>`) | `sender` |
| `actor`, `actor_id`, `action` (service) | sender + `service_action` |
| `text` (str or list of str/entity dicts), `text_entities` | `text` (concatenated plain) + `entities` (prefer `text_entities`) |
| `reply_to_message_id` | `reply_to_tg_message_id` |
| `reply_to_peer_id` (cross-chat replies) | dropped, noted in `media_meta.reply_peer` |
| `forwarded_from`, `forwarded_from_id` | `forward_origin` |
| `media_type`, `mime_type`, `file_name`, `duration_seconds`, `photo`, `poll` | `media_type`, `media_meta` (no file bytes) |
| topic: `reply_to_message_id` pointing at a `topic_created` service message | `message_thread_id` (resolved in a second pass for forums) |

- Unknown fields are ignored but counted (`summary.unknown_fields`) so format drift is visible.
- Batches of 1,000 rows per transaction. Checkpoint the last `id` after each batch to `import_runs.checkpoint`, so `--resume <run_id>` continues after a crash.
- Per-record failures go to `import_errors`, and the run continues. If more than 1% of records fail, the run ends as `partial`.

### 4.4 Dedup against live data
Supergroups: the natural key. If the row already exists from `bot_api`, merge it: add the `desktop_export` source and fill nulls. Text conflicts become versions only when `edited_at` differs. The export is authoritative for messages from other bots.

### 4.5 Report (DM + `/status`)
```
Import #12 — Elysium Early Plays — SUCCEEDED
File result.json (184 MB, sha256 3f2a…)  ·  02:14 elapsed
Records seen 412,880 · new 401,233 · merged 11,402 · unchanged 0
Skipped (service/empty) 231 · Failed 14 (see /import 12 errors)
Range 2025-03-02 09:11 → 2026-11-01 13:58 (Asia/Ho_Chi_Minh)
Coverage now: 2025-03-02 → now · 1 suspected gap (2026-08-10 → 2026-08-12)
```

## 5. Path C — Authorized MTProto backfill via Telethon (Phase 2, optional, **off by default**)

### 5.1 When to use
Use it to fill gaps automatically, or to detect deletions, when a member who is an authorized operator accepts the account-level risk. Path B is always the documented alternative.

### 5.2 Compliance boundaries (non-negotiable)
- Only an account that is **already a legitimate member** of the target group, owned by the operator or a member who explicitly authorized it.
- Never join chats, bypass restrictions, scrape member lists beyond what the group shows, or act on the account's behalf (no sending, reading receipts, or reactions). Read-only history fetch only.
- Use the operator's own `api_id`/`api_hash` (my.telegram.org). Do not use a shared or public `api_id`.
- Respect `FloodWaitError` exactly: sleep the required seconds plus jitter. Never parallelize requests for one account.

### 5.3 Login and session security
- Separate interactive CLI: `elysium-bot mtproto login`. It prompts for phone, code, and 2FA password in the terminal. **Credentials and codes are never sent through the bot chat** and never logged.
- Session: Telethon `StringSession`, encrypted with Fernet (`ELYSIUM_MTPROTO_SESSION_KEY`, 32-byte URL-safe base64), stored at `/data/secrets/mtproto.session.enc` with mode `0600`. The key lives only in env or a Docker secret.
- `elysium-bot mtproto logout` terminates the session server-side (`log_out()`) and deletes the file. The docs explain how to revoke it from Telegram → Settings → Devices.
- The session gives **full account access**. SECURITY.md §4 covers threat handling.

### 5.4 Backfill algorithm
```
resolve entity by tg_chat_id (must already be in account dialogs; else abort)
for target range (gap segment or [oldest, first_observed]):
    iter_messages(entity, reverse=True, offset_id=checkpoint, wait_time=1.0, limit=None)
      → normalize (source=mtproto) → batch 500 → MessageStore.upsert → checkpoint
    on FloodWaitError(s): record, sleep s + rand(1..5), continue
    on ChannelPrivateError / auth errors: stop run, status=failed, alert owner
detect deletions: ID ranges with no message returned inside observed range → mark as `gap_confirmed_empty`
  (known-deleted if previously stored → is_deleted=true, deleted_at=now, source=mtproto)
```
Throughput guidance: start at about 1 request/s (100 messages per request). Document typical flood waits. Run as job family `ingest` so it never overlaps an import.

### 5.5 Operational risks (document in-bot before enabling)
Possible account limits or bans for automated behaviour; a session leak is equivalent to account takeover; Telethon v1 is in maintenance mode (API-layer updates continue); and Telegram may change its terms.

## 6. Coverage and completeness

### 6.1 What is tracked
- `observed` segments per source: contiguous time/ID ranges actually ingested.
- `gap_suspected`: bot outage > 24 h, a failed import range, or a large unexplained ID jump.
- `gap_confirmed_empty`: an MTProto pass confirmed the IDs don't exist (deleted messages).

### 6.2 ID-sequence analysis (supergroups)
IDs are monotonic but not dense, because deleted messages and service events consume IDs. A run of missing IDs is reported as `possible gap` only if it is longer than `max(50, 3 × median gap)` and not covered by a `gap_confirmed_empty` segment. The UI never calls this "missing messages". It says "possible gap (IDs 48,100–48,412 unaccounted for)".

### 6.3 Coverage statement (used everywhere)
`CoverageService.statement(chat, window) -> CoverageStatement` with:
- `earliest_stored_at`, `latest_stored_at`, `messages_in_window`
- `sources_in_window` (counts per source)
- `gaps_in_window` (list)
- `complete_for_window: bool`: true only if the window is fully covered by `observed` segments with no suspected gaps
- `human_text`: e.g. "Stored history covers 2025-03-02 → now. This window is complete." or "⚠️ Partial: no stored messages before 2026-10-28 (bot joined then; no export imported)."

Every analysis, export, and Ask answer embeds this statement. The AI prompt also receives it, with an instruction to repeat the limitation and not claim completeness (AI_MEMORY_ARCHITECTURE.md §7).

## 7. Hierarchical processing of very large histories
After a large import, a `digest_build` job (Phase 6) runs hour → day → week digests over the imported range. It uses Sonnet at `low` effort, checkpointed per period and resumable, and it is budget-gated. The owner confirms the estimated cost first. Imported history stays searchable and exportable with **no AI**. AI digests are optional.

## 8. Tests that must exist (Phase 2)
- Fixture exports: a small supergroup, a forum with topics, a basic group, a full-account export, an old export without `date_unixtime`, and malformed records. All synthetic, never real group data.
- Re-importing the same file twice → zero inserts the second time.
- Export + live overlap → merged sources, no duplicates.
- Edit via live update after import → version 2 created.
- Restart with simulated > 24 h gap → `gap_suspected` created.
- Import resume after a simulated crash mid-batch.
- Timezone: an export from a UTC+7 machine yields correct UTC `sent_at` (uses `date_unixtime`).
- MTProto: a fake Telethon client yields pages and raises `FloodWaitError` once → the sleep is honoured, the checkpoint advances, and no duplicates are written.

## 9. Implementation notes (Phase 2, 2026-11-01)

Decisions made while implementing #2, kept here so later phases build on actual behaviour:

- **The bot's own messages are not persisted.** The Bot API never delivers a bot's outgoing messages as updates. Persisting them (§3.1 "bot_self") needs a send wrapper. Phase 3 only posts the activation notice in groups, so this is deferred to Phase 5, where scheduled summaries are the first substantive bot posts in a group.
- **`$TICKER`s and `@handles` are extracted but not stored yet.** `extraction/tickers.py` exists and is tested. They become `mentions` / `project_aliases` rows in Phase 6, and are always re-derivable from `messages.text`.
- **Basic-group id collisions:** when an export's per-user message id collides with a different stored message, the record is skipped and logged as `import_errors.error_code = basic_group_id_collision`. Fingerprint dedup (§5.3 of DATABASE_DESIGN) handles the normal case. Supergroups are unaffected.
- **MTProto deletions** mark stored messages `is_deleted=true` when they're inside the fetched id range but no longer returned. `gap_confirmed_empty` segments are not written yet. Id ranges that MTProto confirms empty can be added when Phase 9 tightens gap reporting.
- **Consent confirmation** uses a private callback alert (`answerCallbackQuery(show_alert=true)`), which only the tapping member sees. No DM or auto-deleting group reply is needed.
- **Live coverage:** the newest Bot API segment counts as open until "now" unless a `gap_suspected` was recorded after it. Downtime gaps are detected at startup from the previous `polling` heartbeat (> 24 h), and owners get a DM.
- **Imports require a registered chat** (`/activate` first). DM uploads auto-detect the target chat from the export header and run as a background task, with a report on completion. The CLI takes `--chat`/`--force-chat`/`--resume`.
