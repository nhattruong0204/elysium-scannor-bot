# Raw Export Format

Exports are produced by **Export Raw Chat** in the control center (owners and admins; analysts only with `ELYSIUM_ANALYST_CAN_EXPORT=true`). They are delivered **only in the requester's DM** and use **no AI**. Every export is recorded in `audit_log` (`action = export`). At most `ELYSIUM_EXPORT_MAX_PER_HOUR` exports are allowed per user per hour (default 5).

## Formats

| Format | Extension | Layout |
|---|---|---|
| JSON | `.json` | `{"meta": {...}, "messages": [row, ...], "summary": {"row_count": n, "part": k}}` |
| CSV | `.csv` | UTF-8 with BOM, RFC 4180, CRLF. Metadata lines first, each starting with `# ` (pandas: `read_csv(path, comment="#")`), then a header row with the fields below. List fields are JSON-encoded. |
| TXT | `.txt` | Metadata lines, a `====` rule, then `[YYYY-MM-DD HH:MM:SS] #<id> ↩<reply id> <sender>: <text>` per message. Continuation lines are indented 4 spaces. |
| Markdown | `.md` | Title, a metadata list, then one `## YYYY-MM-DD` section per local day with `- **HH:MM** [#id](link) **sender** ↩ [reply]: text` items. |

Timestamps in `sent_at_local` / TXT / Markdown use the group's timezone (default `Asia/Ho_Chi_Minh`). `sent_at_utc` is ISO-8601 UTC.

## Row fields

| Field | Description |
|---|---|
| `chat_id` | Telegram chat id (Bot API form, `-100…`) |
| `tg_message_id` | Message id within the chat |
| `message_link` | `t.me` link (opens only for group members) |
| `sent_at_utc`, `sent_at_local` | Send time |
| `sender_id`, `sender_name`, `sender_username` | Numeric Telegram id, display name at send time, @username. **When pseudonymized:** `sender_id` = `sender_name` = stable per-group pseudonym (`U7`), `sender_username` = null, and `forward_from` = null |
| `reply_to_tg_message_id` | Replied-to message id in the same chat |
| `message_thread_id` | Forum topic / thread root |
| `text` | Message text or media caption |
| `edited_at_utc`, `edit_count` | Latest edit time; number of recorded edits |
| `forward_from` | Name of the forwarded origin, if any |
| `media_type` | `photo`, `document`, `poll`, … (media files are never exported) |
| `is_service` | Service message (joins, pins, …) |
| `links` | URLs found in the message |
| `contract_strings` | `[{value, format, checksum_state}]`: **extracted strings, NOT verified contracts**. `checksum_state` is `valid_checksum`, `all_lower_or_upper`, `invalid_checksum` (likely typo or spoof) or `not_applicable` |
| `sources` | Which ingestion paths saw the message (`bot_api`, `desktop_export`, `mtproto`) |

## Metadata (`meta`)

`generated_at_utc`, `chat_title`, `chat_id`, `window_local`, `window_utc`, `rows_expected`, `filters` (pseudonymized / service messages / bot messages), **`coverage`** (the human-readable coverage statement) and **`coverage_complete`**, `note`, `app_version`, `part`.

`coverage` states what the export contains. It says whether the window is fully covered by stored history, or it is a `⚠️ Partial: …` statement, for example when the window starts before the earliest stored message. **An export never claims to contain the group's full history unless coverage is complete.**

## Size, parts and compression

- Output rotates into parts of at most 40 MB of raw text (`*.partN.<ext>`). A single-part export has no `.partN` suffix.
- Any part larger than 5 MB is deflated into `<name>.zip`.
- Every delivered file stays under Telegram's 50 MB bot upload limit. Each part is a complete, valid file with its own metadata.

## Defaults and filters

Service messages and bot messages are excluded by default and can be toggled on the confirmation screen. Pseudonymization defaults to the group's *Pseudonymize exports* setting.

## Handling

Files are written to `/data/tmp/exports/` with mode 0600, deleted right after sending, and any leftovers are swept at startup. Treat downloaded exports as private group data.

## Search note (PostgreSQL)

On SQLite, FTS5 keeps `$` as a token character, so `$NEST` and `nest` are different terms. On PostgreSQL the `simple` text-search config drops `$`. `$TICKER` queries therefore add an exact substring check, but a plain `nest` query also matches `$NEST`. A custom text-search configuration that aligns the two is planned with the Phase 6 retrieval work.
