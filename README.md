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
| `offload` | Move content out of active context. Deduplicates by content hash. Pass `compress: true` to run through the local model before storing (lossy). Returns a matrix pointer. |
| `offload_path` | Offload a file by path — server reads it, content never enters your context. Strongest form of context displacement. |
| `offload_path_sectioned` | Offload a markdown file split by headings — each section becomes a separate entry. Server reads the file; content never enters context. Headings inside fenced code blocks are not treated as split points. |
| `checkout_remote` | Pull full body, distill via local model, return the distillate fenced as untrusted. Body on disk is unchanged; no checkin required. Returns an error if no local model is configured, the call fails, or the distillation produces more output than the original (expansion guard) — use `checkout` directly in those cases. |
| `pin` | Set salience to 1.0, mark entry pinned. Pinned entries survive `reap` and cleanup passes. |
| `reap` | Delete stale entries by age and/or salience. Pinned entries are always spared. Use `dry_run: true` to preview. |
| `forget` | Permanently delete an entry and its body file. |
| `stats` | Session operation counts and content volume. No "tokens saved" claims — the server cannot see your context window. |

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

The nonce also serves as the checkout token: you must present it to `checkin`. This couples the security handshake to content retrieval.

---

## Deduplication

`offload` and `offload_path` compute a SHA-256 hash of the content before writing. If the same content is already banked under any entry, the duplicate is rejected and the existing pointer is returned.

---

## Cross-session design

The matrix and bodies persist in `~/.local/share/context-hydra/` across sessions. On startup, any entries left in `hot` status from a prior session are reset to `cold` and their stale nonces are cleared.

Min-cold-time tracking (churn warnings after rapid checkout/checkin cycles) is session-scoped and lives in memory only — it doesn't persist, so a fresh session has no artificial cooldown on old entries.

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
cp target/release/context-hydra /usr/local/bin/
# or
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
Content is sent to the configured local model with a summarization prompt before being stored. The body file holds the compressed version. The original is gone — use this only for content where lossy compression is acceptable (completed reasoning traces, resolved error logs, finalized design notes). Silently stores raw if compression is unavailable or if the model's output is longer than the input (expansion guard).

**Checkout-for-remote (`checkout_remote`):**
Pulls the full body, passes it through the local model to produce a token-efficient version, and returns the compressed output fenced as untrusted external data. The body on disk is unchanged. Returns an error if the call fails or the distillation expands the content — use `checkout` directly in those cases. Use this when you need to inject stored content into a remote model's context at minimum cost.

### Configuration

The config file lives in the platform data directory:

- **macOS:** `~/Library/Application Support/context-hydra/config.toml`
- **Linux:** `~/.local/share/context-hydra/config.toml`

```toml
[local_model]
base_url = "http://localhost:8000/v1"
compression_model = "Qwen2.5-7B-Instruct-4bit"  # see model selection below
api_key = ""                                      # leave empty if endpoint requires no auth
compress_target_tokens = 200         # target output length for compress-on-offload
checkout_remote_target_tokens = 300  # target output length for checkout_remote distillation
```

If `[local_model]` is absent or `base_url` is empty, `compress: true` is silently ignored and `checkout_remote` falls back to plain `checkout`.

### Choosing a compression model

The `compression_model` field is for compression and summarization only. A large reasoning model configured for general work is the wrong choice here for two reasons.

**Thinking/reasoning mode must be disabled.** If thinking mode is on, the model spends the entire token budget on reasoning steps and returns empty visible content — the call silently falls back to storing raw and nothing gets compressed.

How to disable thinking:

- **Model variant:** Choose a non-thinking variant. Instruct variants without a "thinking" or "reasoning" suffix typically don't have it enabled by default (e.g. `Qwen2.5-7B-Instruct` rather than `Qwen3-7B`).
- **Server-level setting:** Many local servers (LM Studio, Ollama, etc.) expose a "disable thinking" toggle or `thinking_budget` parameter. Check your server's docs.
- **Dedicated small model:** The simplest option — a separate small model without thinking mode at all.

**A smaller dedicated model is often better than a large one.** Compression is mechanical — "shorten this, keep identifiers and numbers." It does not require large-model capability, and large models are slower. Benchmarked results on four content types (error traces, reasoning chains, design notes, Rust source):

| Model | RAM | Avg ratio | Avg latency | Efficiency | Notes |
|---|---|---|---|---|---|
| `Qwen2.5-3B-Instruct-4bit` | 2.3 GB | 2.4× | 4.3s | **1.04** | Highest efficiency; best on short prose; fails on dense code |
| `Qwen2.5-7B-Instruct-4bit` | 4.7 GB | 2.3× | 8.6s | 0.50 | Best single-model error trace compression (4.8×); also fails on dense code |
| `Qwen2.5-32B-Instruct-4bit` | 18.5 GB | — | — | — | Not benchmarked; expected to handle code better |
| `Llama-3.2-3B-Instruct-4bit` | 2.0 GB | — | — | — | Not benchmarked; widely supported fallback |

Efficiency = average compression ratio / RAM GB. All Qwen Instruct models have thinking mode off by default.

**Code-heavy content needs a larger model.** Both Qwen 3B and 7B trigger the expansion safeguard on Rust source — the compressed output exceeds the input length, so the server stores raw. If your workload includes source files, a 30B+ model with thinking disabled is needed to get meaningful compression — or skip compression on code entirely (`offload` without `compress: true`) and use `checkout_remote` for distillation on retrieval instead.

All models available via `mlx-community` on HuggingFace for Apple Silicon. Run `python3 bench_hydra.py --suggest-models` to see download paths, or `python3 bench_hydra.py --compare-models` to benchmark every model on your local endpoint.

### What the local model is not responsible for

- **Relevance judgment.** context-hydra does not use the local model to decide which stored entries are relevant to the current task — that's the caller's responsibility via `search` and `recall`.
- **Standard offload path.** The default `offload` has no model call, no latency, no dependency. The local model is opt-in per call.
- **Cross-entry synthesis.** `checkout_remote` operates on one entry at a time.

---

## Architecture notes

### Why offload-by-reference is the real win

`offload` takes content already in your context — you've already paid the tokens for it. The net saving is on future sessions that load the matrix instead of the full content.

`offload_path` reads the file on the server side. The content never enters the agent's context at all. This is the only operation that displaces tokens in the current session.

### Why the stats tool doesn't claim "tokens saved"

The server is a stdio process with no visibility into the client's context window, the tokenizer, or transcript state. Any "tokens saved" figure would be a guess.

`bench_hydra.py --window-analysis` does this measurement properly: it queries the live tool schema, measures matrix row sizes from your real store, computes break-even content size per turn, and prints recommended token target settings. Measured baseline on the default tool set: ~1540 tokens schema overhead, ~23 tokens per matrix row, break-even at 3+ turns out of context for content ≥ 400 tokens.
