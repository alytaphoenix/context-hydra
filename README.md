# context-hydra

An MCP server that offloads LLM context to an external store, keeping your active context window lean across long sessions and session boundaries.

Built in Rust. Single binary, zero runtime dependencies, full ACID persistence.

---

## The problem

Long agent sessions accumulate context fast. Past reasoning, file contents, error traces, design notes — they all stay in the window whether you need them right now or not. This inflates token costs, degrades reasoning quality, and means every new session starts blind.

context-hydra gives agents a place to put things down and pick them back up: a compact indexed store with cheap summaries, full-body retrieval on demand, and a trust fence around retrieved content.

---

## How it works

Each offloaded entry has two parts:

- **Matrix row** — a compact index record: ID, type, topic, tags, summary, salience score, status, token estimate
- **Body file** — the full content, stored on disk, never in context unless explicitly checked out

Agents interact via MCP tools. The matrix is always cheap to read; full bodies are gated behind a `checkout`/`checkin` nonce handshake that fences retrieved content as untrusted external data.

---

## Tools

| Tool | Description |
|---|---|
| `peek` | Count-only snapshot (cold/hot/pinned counts, type breakdown). Cheapest orient. |
| `matrix` | Full index sorted by salience. One line per entry. |
| `search` | Filter by keyword, context type, status, or minimum salience. |
| `recall` | Read one entry's summary without pulling the full body. No state change. |
| `checkout` | Pull full body into context. Issues a nonce; marks entry hot. Body is fenced as untrusted. Warns on recent re-checkout (churn signal). |
| `checkin` | Return a checked-out entry. Requires the nonce from `checkout`. Marks entry cold. |
| `offload` | Move content out of active context. Deduplicates by content hash. Pass `compress: true` to run through the local model before storing (lossy — see compression layer). Returns a matrix pointer. |
| `offload_path` | Offload a file by path — server reads it, content never enters your context. Strongest form of context displacement. |
| `checkout_remote` | Pull full body, distill via local model, return the distillate fenced as untrusted. Body on disk is unchanged; no checkin required. Falls back to plain `checkout` if no local model is configured. |
| `pin` | Set salience to 1.0, mark entry pinned. Pinned entries survive `reap` and cleanup passes. |
| `reap` | Delete stale entries by age and/or salience. Pinned entries are always spared. Use `dry_run: true` to preview. |
| `forget` | Permanently delete an entry and its body file. |
| `stats` | Session operation counts and content volume. No fake "tokens saved" claims — the server can't see your context window. |

## MCP Resources

If your MCP client supports resources, these are available without a tool call:

| URI | Description |
|---|---|
| `hydra://matrix` | Compact index of all entries |
| `hydra://body/{id}` | Full body of a specific entry, fenced as untrusted external data |

---

## Trust fence

Bodies retrieved via `checkout` or `hydra://body/{id}` are wrapped in a nonce-tagged fence:

```
<hydra:body:3e7b28a9 id="..." topic="..." offloaded="2026-07-15T10:00:00Z">
...content...
</hydra:body:3e7b28a9>
```

The nonce is random per call and unknown to stored content, making fence breakout structurally hard. Any occurrence of the closing delimiter in the stored content is escaped before delivery.

The nonce also serves as the checkout token: you must present it to `checkin`. This couples the security handshake to content retrieval — you can only check in something you actually received.

---

## Deduplication

`offload` and `offload_path` compute a SHA-256 hash of the content before writing. If the same content is already banked under any entry, the duplicate is rejected and the existing pointer is returned. Topic/summary metadata on the existing entry is not overwritten.

---

## Cross-session design

The matrix and bodies persist in `~/.local/share/context-hydra/` across sessions. On startup, any entries left in `hot` status from a prior session are reset to `cold` and their stale nonces are cleared.

Min-cold-time tracking (churn warnings after rapid checkout/checkin cycles) is intentionally session-scoped and lives in memory only — it doesn't persist, so a fresh session has no artificial cooldown on old entries.

---

## Storage

```
~/.local/share/context-hydra/
  hydra.redb       # matrix index (redb, pure Rust, ACID)
  bodies/
    {uuid}.txt     # one file per offloaded entry
```

redb was chosen over SQLite for zero C FFI. Linear scan over ~500 rows at <1ms — no indexing needed at this scale.

---

## Installation

### Prerequisites

Rust toolchain (stable, 1.80+). Install via [rustup.rs](https://rustup.rs) if you don't have it:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

### Build and install

```bash
git clone https://github.com/alytaphoenix/context-hydra
cd context-hydra
cargo build --release
```

Copy the binary somewhere on your PATH:

```bash
# macOS / Linux
cp target/release/context-hydra /usr/local/bin/

# or install directly from the repo
cargo install --path .
```

### Configure as MCP server

Add to your MCP client config (e.g. `~/.claude/settings.json` for Claude Code):

```json
{
  "mcpServers": {
    "context-hydra": {
      "command": "/path/to/context-hydra",
      "args": []
    }
  }
}
```

### Add to CLAUDE.md (optional but recommended)

Paste the following into any project's `CLAUDE.md` to give agents a protocol for using context-hydra:

```markdown
## Context management (context-hydra MCP)

Call `peek` at the start of every session. If entries exist, call `matrix` to orient.

When to offload: stable content > ~400 tokens you won't need this turn — file contents
after reading, error traces after diagnosing, design decisions after settling.

For files not yet read: use `offload_path` (server reads the file, content never enters context).

Retrieval order (cheapest first): peek → matrix → search → recall → checkout.
Try `recall` before `checkout` — the summary is usually enough.

After processing a checked-out entry, call `checkin` with the nonce.

Maintenance: `pin` high-value entries; `reap` stale ones periodically.
```

---

## Stack

| | |
|---|---|
| Language | Rust (2024 edition) |
| MCP SDK | [rmcp](https://github.com/modelcontextprotocol/rust-sdk) 2.2.0 |
| Database | [redb](https://github.com/cberner/redb) 4.1.0 |
| Transport | stdio |
| Hashing | sha2 0.10 (SHA-256 for dedup) |
| UUIDs | uuid 1.x with v4 |
| HTTP client | reqwest 0.12 (rustls, no system OpenSSL) |
| Config | toml 0.8 |

---

## Local model compression layer

context-hydra can use a local LLM (any OpenAI-compatible endpoint) to compress and summarize content before it leaves the agent context or before it is returned for use with a remote model. This reduces token costs on the remote side without requiring the calling agent to do the compression work itself.

### Two operations

**Compress-on-offload (`offload` with `compress: true`):**
Content is sent to the configured local model with a summarization prompt before being stored. The body file holds the compressed version. The original is gone — use this only for content where lossy compression is acceptable (completed reasoning traces, resolved error logs, finalized design notes). The stored summary field reflects the compressed content, not the original.

**Checkout-for-remote (`checkout_remote`):**
Pulls the full body, passes it through the local model to produce a token-efficient version, and returns the compressed output fenced as untrusted external data. The body on disk is unchanged. Use this when you need to inject stored content into a remote model's context at minimum cost — the remote model sees the distillate, not the raw body.

These map to two specific use cases from kvasir's preprocessing layer:
- **Ledger compression (#1):** `offload(compress: true)` stores a compressed ledger item. kvasir's `[c]ompress` action calls Ornith → `offload` → replaces the ledger item with a matrix pointer.
- **Swarm handoff distillation (#3):** `checkout_remote` pulls a handoff entry and returns a distilled version sized for the receiving swarm node's context budget. The handoff payload is always the distillate, never the raw transcript.

### Configuration

The config file lives in the platform data directory:

- **macOS:** `~/Library/Application Support/context-hydra/config.toml`
- **Linux:** `~/.local/share/context-hydra/config.toml`

```toml
[local_model]
base_url = "http://localhost:8000/v1"
model = "Ornith-1.0-35B-4bit"
api_key = ""                         # leave empty if endpoint requires no auth
compress_target_tokens = 200         # target length for compress-on-offload
checkout_remote_target_tokens = 300  # target length for checkout_remote distillation
```

If `[local_model]` is absent or `base_url` is empty, `compress: true` is silently ignored and `checkout_remote` falls back to plain `checkout`. If the model is configured but the call fails at runtime, `checkout_remote` returns an error rather than creating a hot checkout entry — use `checkout` directly in that case. The server never fails a call solely because compression is unavailable.

### What the local model is not responsible for

- **Relevance judgment.** context-hydra does not use the local model to decide which stored entries are relevant to the current task — that's the caller's responsibility via `search` and `recall`. Compression is mechanical (shorten this); relevance is semantic (which of these matters now).
- **Summarizing before storing (by default).** The standard `offload` path is unchanged — no model call, no latency, no dependency. The local model is opt-in per call.
- **Cross-entry synthesis.** `checkout_remote` operates on one entry at a time. Synthesizing across multiple entries requires the caller to compose them and send to their own model.

---

## Architecture notes

### Why offload-by-reference is the real win

`offload` takes content already in your context — you've already paid the tokens for it. The net saving is on future sessions that load the matrix instead of the full content.

`offload_path` reads the file on the server side. The content never enters the agent's context at all. This is the only operation that displaces tokens in the current session.

### Why the stats tool doesn't claim "tokens saved"

The server is a stdio process. It has no visibility into the client's context window, the tokenizer, or transcript state. Any "tokens saved" figure would be a guess built on unverifiable assumptions. `stats` reports what the server actually knows: operation counts and content volume in bytes.

The real schema overhead measurement (tool descriptions re-sent every request) requires a one-time client-side reading: token count with the server loaded vs. without. That delta is your baseline cost for having the server available.

### Why nonces use 8-char hex

8 hex chars = 32 bits of entropy. Sufficient for a within-session token — the nonce lives only as long as the entry is hot, and collisions within a session would require ~65k concurrent checkouts (birthday bound at 50%). Not a cryptographic guarantee; sufficient for the trust model.
