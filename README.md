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
| `offload` | Move content out of active context. Deduplicates by content hash. Returns a matrix pointer. |
| `offload_path` | Offload a file by path — server reads it, content never enters your context. Strongest form of context displacement. |
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

### Build from source

```bash
git clone https://github.com/alytaphoenix/context-hydra
cd context-hydra
cargo build --release
# binary at target/release/context-hydra
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
