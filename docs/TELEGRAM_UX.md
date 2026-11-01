# Telegram UX — Control Center

Status: **Approved baseline v1**. Library: python-telegram-bot 22.x, `ParseMode.HTML` only.

## 1. Principles

1. **Buttons first.** Every feature is reachable from the main menu. Commands are shortcuts into the same flows.
2. **One live message per flow.** Navigation edits the same message (`edit_message_text`). New messages are sent only for results, long reports, and files.
3. **Every number comes from the DB or a validated analysis.** No placeholder figures in production code paths.
4. **State is visible.** Collection, Backfill, Indexing, and AI each have their own indicator.
5. **Private by default.** The control center lives in the admin's **DM with the bot**. In the group, the bot only answers `/menu` with a deep-link button ("Open control center" → `t.me/<bot>?start=cc_<chat_token>`) and posts scheduled summaries if configured (ADR-012).
6. **Escape everything.** All dynamic text goes through `render.esc()` (`html.escape(…, quote=False)` plus attribute escaping for `href`). Never build HTML by string concatenation outside `telegram_ui/render/`.

## 2. Status vocabulary

| Symbol | Meaning |
|---|---|
| 🟢 | healthy / complete |
| 🟡 | degraded / partial / stale |
| 🔴 | failed / down |
| ⏳ | running (with % when known) |
| ⚪ | disabled / not configured |
| ⚠️ | user-facing caveat (partial history, unverified, cost) |

Emojis are limited to this set plus 📄 (document) and 🔗 (source link). No decorative emoji.

## 3. Callback data and flow state

- Callback data is ≤ 64 bytes. Format: `v1:<op>:<token>[:<arg>]`, e.g. `v1:nav:Kq3x9a:range`, `v1:pg:Kq3x9a:3`.
- `<token>` is a 6–10 char random key into `ui_flows` (user ID, chat ID, flow, JSON state, 30-min TTL, refreshed on use).
- Each callback checks that `callback_query.from_user.id == ui_flows.tg_user_id` and re-checks the role **at execution time**, not just when the menu was drawn. Expired or foreign tokens get "This menu expired — /menu".
- `CallbackRouter` maps `op` → handler. Unknown ops are logged and answered with an alert. Always `answer()` the callback within 1 s (send a toast before long work).
- A per-user throttle of 3 actions/s ignores double taps on mutating buttons.

## 4. Persistent menu

- `set_my_commands` (private scope): `/menu`, `/scan`, `/export`, `/analyze`, `/projects`, `/ask`, `/history`, `/schedule`, `/settings`, `/status`, `/help`.
- Group scope: `/menu`, `/activate`, `/consent`, `/help`.
- `MenuButtonCommands` set for private chats.

## 5. Screens

### 5.1 Main menu (`/menu`, `/start`)

```
<b>ELYSIUM RESEARCH INTELLIGENCE</b>
Group: <b>Elysium Early Plays</b>   ·  tz Asia/Ho_Chi_Minh

Messages stored: <b>12,450</b>  (2025-03-02 → now)
Projects tracked: <b>37</b>  (5 new this week)

🟢 Collection  live · last msg 14:32
🟢 History     export imported · 1 possible gap
🟢 Index       up to date
🟡 AI          last run 12:00 · budget $3.10 / $5.00 left today

[ 🔎 Scan Messages ] [ 🧠 AI Analysis ]
[ 📄 Export Raw Chat ] [ 📊 Research Projects ]
[ 🗂 Chat History ]  [ ⏰ Scheduled Scan ]
[ ⚙️ Settings ]      [ ℹ️ Status ]
```
(The figures are illustrative. The real screen is built from `StatusService.snapshot(chat)`.)

If the user has several chats, a header row shows `[ Group: Elysium Early Plays ▾ ]` to switch the active chat. With exactly one chat it's hidden.
If the user has no role on any active chat, they see: "You don't have access to any research group. Ask the owner."

### 5.2 Time-range picker (shared component `RangePicker`)

```
<b>Select time range</b> — for: Export Raw Chat
[ 1h ] [ 2h ] [ 4h ] [ 12h ]
[ 24h ] [ 7 days ] [ Since last scan ]
[ Entire stored history ]
[ 📅 Custom range ]   [ ⌨️ Type range ]
[ ‹ Back ] [ ⌂ Menu ]
```

- "Since last scan" uses the user's last scan checkpoint for this chat. If there is none, it's disabled (shown with a note).
- "Entire stored history" uses `CoverageStatement.earliest_stored_at → now` and labels it "stored". It is never called "all history" unless coverage is complete.
- **Custom calendar:** month grid with 7 columns and weekday header, `‹ Oct 2026 ›`. Days outside stored coverage are shown as `·` and still selectable. Then an hour grid (6×4) and minutes `[:00] [:15] [:30] [:45]`. Repeat for the end, or choose `[ End = now ]`.
- **Type range:** a ForceReply prompt that accepts `2026-11-01 10:00 → 2026-11-01 14:00`, `yesterday`, `last 3h`, or ISO. It is parsed in the chat tz; errors re-prompt.
- **Confirmation card**, always shown:
```
<b>Range</b>
From  Sun 01 Nov 2026 10:00  (Asia/Ho_Chi_Minh)
To    Sun 01 Nov 2026 14:00
      = 03:00–07:00 UTC
Stored messages in range: 286
⚠️ Partial: no stored messages before 2026-10-28.
[ ✅ Continue ] [ ✏️ Change ] [ ⌂ Menu ]
```

### 5.3 Scan Messages (no AI)
Range → run `ScanService` (job family `ingest`):
1. Ensure index state for the range (drain `pending`).
2. If MTProto is enabled and the range intersects a gap, offer `[ Backfill gap via MTProto ]`.
3. Show the result card: messages, active members, new projects by deterministic alias/contract hits, contract strings shared, top links/domains, coverage, and `[ AI Analysis of this range ] [ Export this range ] [ Source messages ]`.
4. Update the "last scan" checkpoint.

### 5.4 Export Raw Chat (no AI)
Range → format `[ JSON ] [ CSV ] [ TXT ] [ Markdown ]` → options (pseudonymize senders ☑/☐; include service messages ☐; include bot messages ☐) → confirm card (count, est. size, coverage) → progress `⏳ 45%` (edit every ≥ 3 s) → document(s) sent → audit log row.
Shown even when AI is unavailable. The card says "No AI is used."

### 5.5 AI Analysis
Range → mode:
```
[ 📝 Conversation Summary ]
[ 🚀 Elysium Early Plays ]
[ 🔬 Deep Project Research ]
[ 🔄 What's Changed? ]
[ 💬 Ask Research AI ]
```
→ (Deep research: project picker, paginated and searchable by typing) → model:
```
[ ⚡ Sonnet 5.5 — faster, lower cost ✓ ]
[ 🧠 Opus 5.5 — deeper, ~2× cost ]
Effort: [ low ] [ medium ✓ ] [ high ]
```
(The defaults depend on mode: Deep → Opus/high; others → Sonnet/medium.) → **estimate card**:
```
<b>Confirm AI analysis</b>
Mode Elysium Early Plays · Sonnet 5.5 · effort medium
Window 10:00–14:00 (4 h) · 286 messages
Sent to AI 271 · withheld 15 (members without AI consent)
Estimated input ~38k tokens · output ≤ 8k
Estimated cost ≈ $0.16 (budget left today $3.10)
⚠️ Partial history before 2026-10-28
[ ✅ Run ] [ ✏️ Change model ] [ ✖ Cancel ]
```
If the cost exceeds the remaining budget, the Run button is replaced by `[ Reduce range ]` and, for owners only, `[ Override once ]` (audited). After Run, a progress message updates through these stages: `building context → analysing chunk 3/7 → synthesising → validating`.

### 5.6 Report rendering (example: Early Plays)
```
<b>ELYSIUM — EARLY PLAY INTELLIGENCE</b>
Window Nov 1, 10:00–14:00 · 286 messages analysed
Projects mentioned 12 · newly discussed 3
<i>Group-derived research notes. Not financial advice.</i>

<b>New discoveries</b>
<b>1. Project Alpha</b>  🆕 first seen 11:42
Activity rising (2 → 14 mentions, 6 members)
Narrative: DeFi launch on Elysium (claimed)
Evidence: 8 messages 🔗<a href="https://t.me/c/123/48211">1</a> <a href="…">2</a> <a href="…">3</a>
⚠️ Contract shared but not verified (check unavailable)
[ View Project ] [ Source Messages ]
…
<b>Important developments</b> …
<blockquote expandable><b>AI research notes</b>
• Observations derived from group messages
• Claims needing independent verification
• Suggested due-diligence questions</blockquote>
Coverage: complete for window · External lookups: none
[ All Projects ] [ Deep Analysis ] [ 📄 Export Report ] [ ⌂ Menu ]
```
Rules:
- The initial message stays under 3,500 characters. Details are reachable by buttons (project cards, source pages) and an expandable blockquote.
- Counts ("12 projects", "286 messages", "8 messages") are **recomputed from DB rows** linked to the run, not copied from model output.
- "🆕 first seen" appears only when `projects.first_mentioned_at` falls inside the window. If coverage before the window is partial, it reads "🆕 new to stored history".
- Source links: `https://t.me/c/<internal_id>/<msg_id>` (private supergroup; `internal_id` = `tg_chat_id` without `-100`), `https://t.me/c/<id>/<thread>/<msg>` for forum topics, or `https://t.me/<username>/<msg>` for public groups. Links only open for members, which is intended.
- The footer always states the coverage and the tools actually used ("External lookups: none" / "On-chain: Blockscout (checked 13:55)").
- Reports over the limit: send a short card plus `report_<chat>_<run>.md` (and `.html`) as a document.

### 5.7 Research Projects
Paginated list (8 per page): `name · state · mentions 7d · last seen · risk badge`. Filters: `[ New ] [ Rising ] [ Watching ] [ All ] [ 🔎 Search ]`.
Project card: aliases, tickers and contract strings with status, links with claimed/verified status, timeline (latest 5, `[More]`), open claims with status, risk flags, and `[ Deep Research ] [ Timeline ] [ Claims ] [ Sources ] [ Set state ▾ ] [ Merge… ]`. Admin-only: `Merge`, `Verify link`, `Add alias`, `Set state`.

### 5.8 Chat History
Coverage dashboard: segments per source, gaps, last import runs, and `[ Import export (upload) ] [ MTProto backfill ] [ Import errors ] [ Search messages ]`.
Search: ForceReply query → FTS → paginated results with snippet, sender pseudonym or name (per setting), time, and 🔗 link.

### 5.9 Scheduled Scan
```
Automatic collection  [ Off ] [ 1h ] [ 2h ] [ 3h ] [ 4h ]
AI summary            [ Off ] [ 1h ] [ 2h ] [ 3h ] [ 4h ]
Mode                  [ Summary ✓ ] [ Early Plays ]
Model                 Sonnet 5.5 (default)
Min new messages      [ 10 ] [ 20 ✓ ] [ 50 ] [ 100 ]
Destination           [ Group ] [ My DM ✓ ]
Daily AI budget       $5.00  [ Change ]
Next run 16:00 · last run 14:00 (skipped: 7 < 20 msgs)
```
Note: "Automatic collection" only matters when MTProto gap-fill is enabled. Live Bot API collection is always on. The screen explains this in one line.

### 5.10 Settings
Timezone (common list plus typed IANA name, validated with zoneinfo), default models/effort, AI consent policy (owner), retention days (owner), export pseudonymization default, display names to AI (off by default), roles (`[ Manage access ]`: list, add by forwarding a user's message or entering their numeric ID, remove), and danger zone (owner): deactivate chat, purge member data, delete all chat data, each with two-step confirmation and a typed chat title.

### 5.11 Status
Components (polling heartbeat, scheduler, job runner), last update received, running jobs with `[ Cancel ]`, recent failures, DB size, last backup, versions (app, schema, prompt, skills), and configured integrations (⚪/🟢 for Anthropic, MTProto, on-chain providers).

### 5.12 Ask Research AI
`[ 💬 Ask ]` → ForceReply "Ask about the group's research…" → typing indicator → answer:
```
<b>Q:</b> Which contracts were shared in the past 24h?
<b>A:</b> Three EVM-format contract strings were shared … [1][2][3]
• 0xAbc…123 — shared by U12 at 10:41 as "Alpha token" [1]; status: not verified
…
Sources: [1] [2] [3]  ·  Searched: group messages (24h window)
External lookups: none · Coverage: complete for window
[ Ask follow-up ] [ Sources ] [ ⌂ Menu ]
```
Follow-ups keep the last 3 Q/A turns as context (stored in `ui_flows`). Each answer re-runs retrieval.

## 6. Group-side behaviour
- `/activate` (owner): activation checks plus the participant notice (SECURITY.md §3).
- `/consent`: a member grants or revokes AI consent with buttons `[ I consent ] [ I do not consent ] [ What does this mean? ]`. The bot confirms in DM if possible, otherwise with a short reply that auto-deletes after 30 s.
- Scheduled summaries posted to the group are **short** (≤ 1,500 chars) and contain no export data. A full report is available via a DM deep link for authorized users.
- The bot never replies to arbitrary group chatter.

## 7. Limits and errors
- Text limit 4,096 chars per message; captions 1,024; file upload 50 MB via Bot API; file download 20 MB.
- Outgoing rate: `AIORateLimiter` (≈ 20 msgs/min/group, 30/s global). Progress edits are throttled to ≥ 3 s.
- `render.split_html(text, limit=4000)` splits at section boundaries, never inside a tag or entity. Tested with nested `<b><a>` and expandable blockquotes.
- Errors to users are short and actionable ("Anthropic API is unreachable — exports and search still work. Retry later."). Stack traces go only to logs.

## 8. UI tests (Phase 3)
- The callback codec round-trips and stays ≤ 64 bytes for all ops (property test).
- A foreign user pressing a button → rejected; an expired token → "expired" message.
- RangePicker presets produce correct UTC bounds across the `Asia/Ho_Chi_Minh` midnight boundary; month navigation handles Dec→Jan.
- `split_html` never produces invalid HTML (parse each chunk with `html.parser`).
- Snapshot tests for main menu, estimate card, and report card rendering, using fixed DB fixtures.
