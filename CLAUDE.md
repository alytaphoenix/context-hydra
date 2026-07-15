# context-hydra

## Working on this codebase

Rust MCP server. Single binary, stdio transport, redb + flat files.

- Matrix index: `~/.local/share/context-hydra/hydra.redb`
- Body files: `~/.local/share/context-hydra/bodies/{id}.txt`
- MCP SDK: rmcp 2.2.0 — tool methods need `#[tool]` + `#[tool_router]`/`#[tool_handler]` macros
- `store.rs` — DB + file ops, `MatrixRow` schema
- `server.rs` — all MCP tools and resources

Build: `cargo build`. Test with piped JSON-RPC on stdin.

---

## Template: paste into any project's CLAUDE.md to enable context-hydra

```markdown
## Context management (context-hydra MCP)

You have a context-hydra MCP server. Use it to keep your active context lean.

### Session startup

Call `peek` at the start of every session.
- Empty → proceed normally
- Entries exist → call `matrix` to orient, then decide what to recall or checkout

### When to offload

Offload content that is **stable and won't change this session**:
- Design decisions you've settled
- File contents you've read and finished editing
- Long error traces after you've diagnosed them
- Research findings after you've summarized them
- Any block > ~400 tokens you might need later but don't need right now

**For files you haven't read yet: use `offload_path` instead of reading then offloading.**
`offload_path` reads the file on the server — the content never enters your context at all.
This is the strongest form of context displacement available.

**For large markdown reference docs (CLAUDE.md, AGENTS.md, architecture notes): use `offload_path_sectioned`.**
It splits the file on headings and stores each section as a separate entry, so you can
recall or checkout only the section you need. Pass `pin: true` to preserve sections across
reap cycles. The filename is auto-tagged so `search(query: "CLAUDE.md")` finds all sections.
On re-runs against a changed file, use `search` then `forget` to clear stale sections first.

### Retrieving context

Work cheapest-first:
1. `peek` — count-only snapshot, ~2 tokens output
2. `matrix` — full index, one line per entry
3. `search` — filter by keyword, type, or salience
4. `recall` — one entry's summary, no body pull
5. `checkout` — full body (use nonce to `checkin` when done)

**Always try `recall` before `checkout`.** The summary is usually enough.

### Checkin discipline

After working with a checked-out entry, call `checkin` with the nonce.
If you check out the same entry again soon, you'll see a churn warning — that's a signal
to use `recall` instead.

### Maintenance

- `pin` high-value entries to protect them from reap and cleanup
- `reap` to clear stale entries:
  - Dry run first: `reap(max_age_days: 7, max_salience: 0.3, dry_run: true)`
  - Then commit: `reap(max_age_days: 7, max_salience: 0.3)`
- `forget` to remove a specific entry permanently

### Tool reference

| Goal | Tool |
|---|---|
| Orient quickly | `peek` |
| See all entries | `matrix` |
| Find by topic | `search` |
| Read summary | `recall` |
| Pull full body | `checkout` → work → `checkin` |
| Move content out (in context) | `offload` |
| Move file out (not in context) | `offload_path` |
| Split markdown file by headings | `offload_path_sectioned` |
| Preserve across sessions | `pin` |
| Remove stale entries | `reap` |
| Remove permanently | `forget` |
| Session diagnostics | `stats` |
```
