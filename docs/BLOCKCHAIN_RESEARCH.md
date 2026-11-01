# Blockchain Research Framework & Skills

Status: **Approved baseline v1**. Implemented in Phase 7, with Phase 8 consuming it.

## 1. Research posture

The analyst persona is a **disciplined senior blockchain researcher with a 16-year market-analysis perspective**: evidence first, sceptical of narrative-driven hype, aware of recurring cycle patterns (launch-day liquidity games, impersonation around hyped launches, points/airdrop speculation, "wen token" rumours before mainnets), and focused on risk before upside. The persona is analytical framing only. Outputs never claim personal experience, never recommend buying or selling, and never predict returns.

Priority ranking for "what deserves research" uses transparent factors only:

| Factor | Signal source | Weight (configurable) |
|---|---|---|
| Novelty | first seen in stored history within N days | 0.20 |
| Attention acceleration | mention-rate ratio (window vs trailing 7d), distinct mentioners | 0.20 |
| Independent corroboration | ≥ 2 distinct members, not reply-chained to one promoter | 0.15 |
| Evidence quality | official links/registry match, on-chain existence | 0.20 |
| Catalyst proximity | dated claims (launch, TGE, mainnet) within 14 days, labelled claimed/verified | 0.10 |
| Open risk (negative) | open high/medium risk flags | −0.15 |

The score is computed **in code** from DB rows (`research/priority.py`) and shown with its factor breakdown. The model may explain it but never sets it. "Message volume ≠ investment quality" appears in the Early Plays footer.

## 2. Skill framework

### 2.1 Directory contract
```
skills/<skill-name>/
  SKILL.md          # frontmatter + instructions (prompt module)
  schema.py         # pydantic output model(s) the skill contributes (imported by ai/schemas.py)
  checks.py         # optional deterministic checks (no network unless via onchain/ providers)
  fixtures/         # synthetic inputs + expected outputs
  test_skill.py     # unit tests (deterministic) + optional live_eval tests
  CHANGELOG.md
```
`SKILL.md` frontmatter:
```yaml
---
name: token-due-diligence
version: 1.0.0
purpose: Structured due-diligence checklist for a token/contract discussed in the group
modes: [early_plays, deep_research, ask]      # where the registry includes it
inputs: [context_pack.project, context_pack.tokens, onchain_checks]
outputs: [TokenDDSection]
requires: [onchain-risk-analysis]             # composition order
limitations: >
  Cannot verify contracts without a configured provider; never infers liquidity or holders.
---
```

### 2.2 Runtime loading (ADR-004)
`ai/skill_registry.py` loads `skills/*/SKILL.md` at startup, validates the frontmatter, and topologically sorts by `requires`. For a mode it concatenates the matching modules **in stable order** into the cached system prompt section `<research_skills>`. The skill version set goes into `prompt_version`. Skills cannot add tools or change the safety rules in the base system prompt; the registry rejects modules containing override phrases (a lint test).

### 2.3 Testing a skill
- Deterministic: `checks.py` against fixtures, and schema round-trip.
- Prompt-lint: no forbidden phrases ("buy", "guaranteed", "will pump", "verified" without an evidence clause), and length within budget.
- Live eval (opt-in): run the fixture through the mode with a fake-free client and assert schema validity plus key assertions.

## 3. The seven skills

### 3.1 `elysium-ecosystem`
- **Purpose:** ecosystem grounding. What Elysium is, official sources, chain facts, launch status, and known name collisions.
- **Inputs:** `skills/elysium-ecosystem/registry.yaml` (human-curated, every fact has `source_url`, `source_type: official|secondary`, `verified_at`, `status`).
- **Method:** classify each project's `elysium_relation`; flag claims that contradict registry facts; label pre-mainnet speculation.
- **Output:** `EcosystemContext { relation, registry_matches[], contradictions[], prelaunch_flags[] }`.
- **Limitations:** the registry is only as current as its last `verified_at`. Stale entries (> 14 days) are labelled.
- **Seed registry (to verify against official sources during Phase 7; do not mark `official` until checked):**

| Fact | Value | Status at 2026-11-01 | Source type |
|---|---|---|---|
| Elysium | Hyperliquid-aligned L2 by Kinetiq; Arbitrum Orbit chain settling to HyperEVM; HYPE as gas | reported | secondary (Bankless, Chainstack docs, The Industry Spread) |
| Mainnet date | 2026-11-09 | **announced, unconfirmed**; Kinetiq says the timetable may change | secondary (Bankless, KuCoin, ChainCatcher) |
| Testnet chain ID | 99801 (parent HyperEVM testnet 998) | reported | secondary (Chainstack, Alchemy docs) |
| Testnet explorer | `elysium.kinetiq.xyz/testnet-explorer` (Blockscout API `elysium.kinetiq.xyz/api`); a Conduit-hosted explorer is also cited | reported, sources conflict | secondary |
| Mainnet chain ID / RPC / explorer | not published | unknown | — |
| Launch partners | Nest (DEX, "live from first block"), Speculate, Dawn | claimed by Kinetiq ecosystem page per press | secondary |
| Sequencer revenue share | 25% to builders | reported | secondary |
| Native Elysium token / points | none announced (per one report) | reported | secondary |
| **Name collision** | viem's `elysiumTestnet` = unrelated Vulcan Forged chain (ID 1338, LAVA) | fact | Alchemy docs |

The registry also tracks **launch-date watch** entries. A `launch_watch` job (Phase 7, daily until 2026-11-30) asks admins to confirm or update the mainnet status. Any group claim about the date is compared to the registry and labelled.

### 3.2 `early-project-discovery`
- **Purpose:** find projects and extract identity fields without inventing any.
- **Inputs:** window messages, known-projects list, deterministic hits.
- **Method:** extract name, aliases, ticker, chain hint, contract strings, website/docs/social/Telegram links, and first/last mention (**computed in code**). Unknown fields stay `null` with `"not mentioned"`.
- **Output:** `ProjectDiscovery[]` (each field carries citations or is null).
- **Limitations:** cannot discover projects never named in text (e.g. image-only shills).

### 3.3 `token-due-diligence`
- **Purpose:** a structured DD checklist per token/contract.
- **Method:** a fixed checklist (identity, official affiliation, contract status, deployer/ownership, liquidity, holder concentration, transfer restrictions, audits, team, tokenomics claims). Each item is `known (evidence)`, `claimed (citation)`, `unavailable (no provider)`, or `unknown`.
- **Output:** `TokenDDSection`.
- **Limitations:** depends on `onchain-risk-analysis` checks. Audits count only when an auditor-hosted link is recorded.

### 3.4 `narrative-and-social-sentiment`
- **Purpose:** what narratives drive attention, and how they evolve.
- **Method:** cluster messages into narratives ("Elysium native perps", "HYPE gas plays", "points farming"); compute attention metrics **in code** (mentions/hour, distinct mentioners, reply depth); the model labels and describes clusters, with supportive vs opposing views.
- **Output:** `NarrativeSection { narratives[], stance_balance, shifts_since_last }`.
- **Limitations:** sentiment applies only to this group's members and is not market-wide.

### 3.5 `onchain-risk-analysis`
- **Purpose:** run and interpret the deterministic check catalogue (§5).
- **Method:** call the configured `OnchainProvider`s via `checks.py`. The model only **interprets** stored results and never produces them.
- **Output:** `OnchainRiskSection { checks: [ {name, status, summary, checked_at, provider} ] }`.
- **Limitations:** EVM-format addresses exist on many chains. "Found on X" never implies "official".

### 3.6 `project-history-tracking`
- **Purpose:** maintain timelines and "what changed".
- **Method:** consume the code-computed L2/L3 diff since the watermark; explain new events, reversed opinions, resolved questions, and corrected misinformation.
- **Output:** `HistorySection { new_events[], reversals[], resolved_questions[], corrections[] }`.
- **Limitations:** reliable only over stored coverage, so it repeats coverage gaps.

### 3.7 `shill-and-claim-validation`
- **Purpose:** trace promotional claims to their origins and evidence.
- **Method:** for each claim: first claimant (from code), evidence provided (links, contract, screenshots mentioned), later support or contradiction, whether claims mutated over time (statement diffs across superseding claims), and conflicting opinions. Behavioural patterns (e.g. "same member introduced 5 unverified tokens in 48 h") are reported **as observations with citations**, never as character judgements.
- **Output:** `ClaimValidationSection { claims[]: {statement, claimant, status, evidence_summary, mutation_history, open_questions} }`.
- **Limitations:** it cannot detect off-group coordination, and there are no "scammer" labels (SECURITY.md §8).

### 3.8 Mode → skill composition

| Mode | Skills (in order) |
|---|---|
| Conversation Summary | elysium-ecosystem, early-project-discovery (light) |
| Elysium Early Plays | elysium-ecosystem, early-project-discovery, narrative-and-social-sentiment, shill-and-claim-validation, onchain-risk-analysis |
| Deep Project Research | all seven |
| What's Changed | project-history-tracking, shill-and-claim-validation, elysium-ecosystem |
| Ask | elysium-ecosystem, plus others selected by the query planner (e.g. contract question → onchain-risk-analysis) |

## 4. Deterministic extraction (Phase 2 core, used by skills)

- **EVM addresses:** `(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])`. EIP-55 state: mixed case with valid checksum → `valid_checksum`; all lower or all upper → `all_lower_or_upper` (no checksum info); mixed case with an invalid checksum → `invalid_checksum` (a strong typo/scam signal, which raises risk flag `address_checksum_mismatch`).
- **Look-alike detection:** two addresses in the same chat sharing the first 4 and last 4 hex chars but differing elsewhere → risk flag `lookalike_address` (address-poisoning pattern).
- **Base58-like strings** (32–44 chars) are captured as `base58_like` but lower confidence, since they're not Elysium-relevant. Excluded from reports unless a project links them.
- **Tickers:** `\$[A-Za-z][A-Za-z0-9]{1,9}\b`. Bare uppercase words are not tickers unless they're known aliases.
- **Links:** entity URLs plus regex. Domains are normalized (punycode decoded and flagged when it differs from the displayed text, which indicates an IDN homograph).
- **Handles:** `@name` and `x.com/<name>` / `twitter.com/<name>` / `t.me/<name>`.

## 5. Risk check catalogue

| Check | Type | Data needed | Status if unavailable |
|---|---|---|---|
| `address_checksum_mismatch` | deterministic | message text | n/a (always available) |
| `lookalike_address` | deterministic | stored contract strings | n/a |
| `multiple_contracts_for_ticker` | deterministic | tokens table | n/a |
| `impersonation_link` | deterministic + registry | links vs registry official domains/handles (edit distance ≤ 2, homographs) | n/a |
| `unclear_official_affiliation` | registry | registry + project_links | n/a |
| `prelaunch_speculation` | registry | mainnet status + claim dates | n/a |
| `rumor_as_fact` | AI-proposed, code-confirmed | claim status `unverified` phrased as certain | n/a |
| `unsupported_valuation_claim` | AI-proposed | claim_type valuation_price, no evidence | n/a |
| `contract_exists` | on-chain | JSON-RPC `eth_getCode` per configured chain | `unavailable` |
| `contract_source_verified` | on-chain | Blockscout `/api/v2/smart-contracts/{addr}` | `unavailable` |
| `token_metadata` (name/symbol/decimals/supply) | on-chain | Blockscout `/api/v2/tokens/{addr}` | `unavailable` |
| `holder_concentration` (top-10 share) | on-chain | Blockscout `/api/v2/tokens/{addr}/holders` | `unavailable` |
| `deployer_and_owner` | on-chain | Blockscout address/creation tx; `owner()` call | `unavailable` |
| `transfer_restrictions_honeypot` | simulation | needs a simulation provider (none configured in MVP) | **`unavailable` by design in MVP** |
| `liquidity_conditions` | market data | DEX/indexer API supporting Elysium (none confirmed at launch) | `unavailable` |

Rules: results are stored in `onchain_checks` with `checked_at` and `expires_at` (default 6 h for market-ish data, 7 d for code/verification). Displays always show age. If a provider isn't configured, the result is `unavailable` with the reason, never a guess. Provider endpoints are config (`ELYSIUM_ONCHAIN_PROVIDERS_JSON`), because mainnet endpoints are not yet published.

### 5.1 Provider interface
```python
class OnchainProvider(Protocol):
    name: str
    chains: set[str]
    async def get_code(self, chain: str, address: str) -> ProviderResult: ...
    async def get_contract_info(self, chain: str, address: str) -> ProviderResult: ...
    async def get_token_info(self, chain: str, address: str) -> ProviderResult: ...
    async def get_top_holders(self, chain: str, address: str, limit: int = 10) -> ProviderResult: ...
# ProviderResult.status ∈ {ok, not_found, unsupported, error}; raw payload kept (trimmed) for audit
```
Implementations: `JsonRpcProvider` (any EVM RPC) and `BlockscoutProvider` (v2 REST). Both use httpx with timeouts (5 s), retry ×2, and a per-provider rate limit. Tests use respx mocks only; CI never hits real endpoints.

## 6. External verification (Phase 8, optional)
- Optional Anthropic server tools `web_search_20260209` (and `web_fetch_20260209`) with `allowed_domains` limited to registry official domains plus configured research domains. Off by default (`ELYSIUM_WEB_VERIFICATION=false`).
- `tools_used` is derived from response content blocks (`server_tool_use` / `web_search_tool_result`). Reports print "External lookups: web search (3 queries)" only when such blocks exist.
- Findings from the web are stored as `claim_evidence(external_url=…, relation=supports|contradicts)`. They become `official_source` only if the URL's domain is in the registry's official list.
- Web fetch is disabled unless explicitly enabled, because it is the main prompt-injection exfiltration vector (SECURITY.md §7).

## 7. Output language
- Every analytical item answers "what is claimed, by whom, with what evidence, what is unknown".
- Required phrases: "claimed in group", "not verified", "check unavailable", "new to stored history".
- Forbidden (enforced by a post-generation lint that rewrites or flags): "guaranteed", "will moon/pump", "safe to buy", "buy now", "scam" applied to a named person, and price targets.
