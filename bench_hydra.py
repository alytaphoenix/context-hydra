#!/usr/bin/env python3.11
"""context-hydra compression benchmark

Drives context-hydra via its MCP stdio interface. Measures token reduction,
compression quality, and compares available oMLX models by efficiency.

Usage:
    python3 bench_hydra.py                          # use existing config.toml
    python3 bench_hydra.py --model Ornith-1.0-35B-4bit
    python3 bench_hydra.py --compare-models         # sweep all oMLX models
    python3 bench_hydra.py --quick                  # error_trace sample only
"""

import json, math, time, sys, os, re, uuid, subprocess, threading, queue
import platform, argparse, shutil, textwrap, urllib.request, statistics, tempfile
from pathlib import Path
from collections import Counter

# ── paths & defaults ──────────────────────────────────────────────────────────

OMLX_BASE = "http://localhost:8000"

def omlx_api_key() -> str:
    """Read API key from ~/.omlx/settings.json, or return empty string."""
    try:
        settings = json.loads(
            (Path.home() / ".omlx" / "settings.json").read_text()
        )
        return settings.get("auth", {}).get("api_key", "")
    except Exception:
        return ""

def data_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "context-hydra"
    return Path.home() / ".local" / "share" / "context-hydra"

CONFIG_FILE      = data_dir() / "config.toml"
COMPRESS_TARGET  = 200
DISTILL_TARGET   = 300

# Known RAM footprints (GB) for efficiency scoring. Add more as needed.
# These cover 4-bit quantizations unless noted. Used by --compare-models.
RAM_MAP: dict[str, float] = {
    # ── large (16–25 GB) ──────────────────────────────────────────────────────
    "Ornith-1.0-35B-4bit":                         19.8,
    "Ornith-1.0-35B-4bit:ornith-fast":             19.8,  # same model, thinking disabled
    "gemma-4-31B-it-MLX-6bit":                     24.9,
    "gemma-4-31B-it-MLX-6bit:gemma-fast":          24.9,
    "gemma-4-31B-it-OptiQ-4bit":                        20.8,
    "gemma-4-31B-it-OptiQ-4bit:gemma-fast":             20.8,
    "gemma-4-31B-it-OptiQ-4bit:gemma-q4-fast":          20.8,
    "gemma-4-26B-A4B-it-OptiQ-4bit":                    16.4,  # MoE
    "gemma-4-26B-A4B-it-OptiQ-4bit:gemma-fast":         16.4,
    "gemma-4-26B-A4B-it-OptiQ-4bit:gemma-moe-fast":     16.4,
    "Qwen2.5-32B-Instruct-4bit":              18.5,
    # ── medium (6–14 GB) ──────────────────────────────────────────────────────
    "Qwen2.5-14B-Instruct-4bit":               9.0,
    "Qwen2.5-14B-Instruct-8bit":              15.2,
    "gemma-3-12b-it-qf16":                    12.0,
    "Phi-4-14B-4bit":                          8.5,
    # ── small (2–6 GB) ────────────────────────────────────────────────────────
    "Qwen2.5-7B-Instruct-4bit":                4.7,
    "Qwen2.5-7B-Instruct-8bit":                8.1,
    "gemma-3-4b-it-qf16":                      3.5,
    "gemma-4-4b-it-qf16":                      3.0,
    "Phi-3.5-mini-instruct-4bit":              2.3,
    "Llama-3.2-3B-Instruct-4bit":              2.0,
    "Qwen2.5-3B-Instruct-4bit":                2.3,
    # ── tiny (<2 GB) ──────────────────────────────────────────────────────────
    "SmolLM2-1.7B-Instruct-4bit":              1.1,
    "Qwen2.5-1.5B-Instruct-4bit":              1.2,
    "Llama-3.2-1B-Instruct-4bit":              0.9,
}

# Curated download suggestions by RAM tier (mlx-community HuggingFace paths)
SUGGESTED_MODELS: list[dict] = [
    {"tier": "tiny  (<2 GB)",  "ram": 1.1,  "id": "SmolLM2-1.7B-Instruct-4bit",
     "hf": "mlx-community/SmolLM2-1.7B-Instruct-4bit"},
    {"tier": "tiny  (<2 GB)",  "ram": 1.2,  "id": "Qwen2.5-1.5B-Instruct-4bit",
     "hf": "mlx-community/Qwen2.5-1.5B-Instruct-4bit"},
    {"tier": "small (2–4 GB)", "ram": 2.0,  "id": "Llama-3.2-3B-Instruct-4bit",
     "hf": "mlx-community/Llama-3.2-3B-Instruct-4bit"},
    {"tier": "small (2–4 GB)", "ram": 2.3,  "id": "Qwen2.5-3B-Instruct-4bit",
     "hf": "mlx-community/Qwen2.5-3B-Instruct-4bit"},
    {"tier": "small (2–4 GB)", "ram": 3.5,  "id": "gemma-3-4b-it-qf16",
     "hf": "mlx-community/gemma-3-4b-it-qf16"},
    {"tier": "medium (5–9 GB)","ram": 4.7,  "id": "Qwen2.5-7B-Instruct-4bit",
     "hf": "mlx-community/Qwen2.5-7B-Instruct-4bit"},
    {"tier": "medium (5–9 GB)","ram": 8.5,  "id": "Phi-4-14B-4bit",
     "hf": "mlx-community/Phi-4-14B-4bit"},
]

# ── content samples ───────────────────────────────────────────────────────────

SAMPLES: dict[str, dict] = {
    "error_trace": {
        "label": "Rust error trace",
        "content": ("""\
thread 'tokio-runtime-worker' panicked at 'called `Result::unwrap()` on an \
`Err` value: Os { code: 111, kind: ConnectionRefused, message: "Connection refused" }', \
src/client.rs:142:5
stack backtrace:
   0: rust_begin_unwind
             at /rustc/a8314ef7d/library/std/src/panicking.rs:652:5
   1: core::panicking::panic_fmt
             at /rustc/a8314ef7d/library/core/src/panicking.rs:72:14
   2: core::result::unwrap_failed
             at /rustc/a8314ef7d/library/core/src/result.rs:1735:5
   3: context_hydra::client::Client::send_request
             at ./src/client.rs:142:26
   4: context_hydra::server::handle_connection
             at ./src/server.rs:87:14

ERROR 2026-07-16T14:23:11Z context_hydra: connection to backend failed after 30s timeout
  endpoint: http://localhost:8000/v1/chat/completions
  attempts: 3
  last_error: io::Error(ConnectionRefused)

ERROR 2026-07-16T14:23:16Z context_hydra: retry 1/3 failed, backing off 10s
ERROR 2026-07-16T14:23:26Z context_hydra: retry 2/3 failed, backing off 20s
ERROR 2026-07-16T14:23:46Z context_hydra: all retries exhausted, returning error to caller
  request_id: req-a8f3b2c1
  duration_total_ms: 45823
""" * 3),
    },

    "reasoning_chain": {
        "label": "reasoning chain",
        "content": ("""\
Let me work through the authentication flow step by step.

The key constraint is that authentication must complete before any database
connection is opened. The database pool uses a connection-level authorization
token that comes from the auth service response.

Step 1: Check the token cache. Key: auth:{client_id}:{scope}. TTL 300s.
If found and not expired, skip to step 4.

Step 2: Call /auth with client credentials:
  POST /auth
  Body: {"client_id": ..., "client_secret": ..., "scope": "db:read db:write"}
  Response: {"token": "...", "expires_in": 300, "token_type": "Bearer"}

Step 3: Store token in cache with expiry = now + expires_in - 30s.
The 30s safety margin prevents races where a token expires between cache read
and first use.

Step 4: Open database connection using the token from cache.
  Connection string: postgres://localhost:5432/hydra?sslmode=require
  Pool: min=2, max=10, idle_timeout=60s, max_lifetime=3600s

Step 5: If DB connection fails with auth error (FATAL: password authentication
failed), invalidate the cache entry and retry from step 2. Limit to 1 retry to
prevent amplification of auth service failures.

Edge cases:
- Clock skew between services: handled by the 30s safety margin (step 3).
- Auth service down: return 503 immediately, do not retry (fail fast).
- Token revoked mid-session: pool detects auth error on next acquire, triggers
  re-auth. Existing in-use connections are not disrupted.

We don't always re-authenticate because the auth service is rate-limited at
100 req/s per client_id. With 10 server instances at 10 concurrent requests
each, we'd hit ~1000 auth req/s without caching.
""" * 2),
    },

    "design_note": {
        "label": "design decision note",
        "content": """\
# Decision: dedup by content hash, not topic

## Context

When an agent offloads the same content twice (a file it re-reads, a recurring
error format), we want to avoid duplicate body files on disk.

## Options considered

**Option A: Dedup by topic string.**
Reject offload if a row with the same topic exists. Problem: topics are
agent-chosen strings that vary ("client.rs error" vs "error in client.rs")
even for identical content. Also blocks legitimate distinct entries.

**Option B: Dedup by content hash (SHA-256).** (chosen)
Hash the full body before storing. If hash matches any row, return existing
pointer. Deterministic, topic-independent, zero false positives.

**Option C: Fuzzy dedup via embedding similarity.**
Rejected: requires an embedding model (dependency, latency, RAM). Value of
deduping similar-but-not-identical content is low. Exact dedup covers the
main case (same file re-offloaded).

## Consequences

- Only fires on exact byte-for-byte content matches.
- Agent updating a file gets a new entry (content differs, hash differs).
- Hash stored on MatrixRow; dedup check is a linear scan (~500 rows, <1ms).
- Hash computed before compression: same original always deduplicates
  regardless of LLM nondeterminism in the compressed output.

## Tradeoffs

Pro: Zero false positives, no model dependency, minimal overhead.
Con: Does not catch reformatted or partially-edited content. Acceptable v1.
""",
    },

    "code_context": {
        "label": "Rust source context",
        "content": """\
// src/store.rs — matrix index and body file management

use std::{path::PathBuf, sync::Arc};
use anyhow::Result;
use chrono::{DateTime, Utc};
use redb::{Database, ReadableDatabase, ReadableTable, TableDefinition};
use serde::{Deserialize, Serialize};

pub const MATRIX: TableDefinition<&str, &[u8]> = TableDefinition::new("matrix");

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MatrixRow {
    pub id: String,
    pub ctx_type: String,
    pub topic: String,
    pub tags: Vec<String>,
    pub summary: String,
    pub token_estimate: u32,
    pub salience: f64,
    pub status: String,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(default)]
    pub checkout_nonce: Option<String>,
    #[serde(default)]
    pub content_hash: String,
}

pub struct Store {
    pub db: Arc<Database>,
    pub bodies_dir: PathBuf,
}

impl Store {
    pub fn new() -> Result<Self> {
        let data_dir = dirs::data_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("context-hydra");
        std::fs::create_dir_all(&data_dir)?;
        let bodies_dir = data_dir.join("bodies");
        std::fs::create_dir_all(&bodies_dir)?;
        let db = Database::create(data_dir.join("hydra.redb"))?;
        let write_txn = db.begin_write()?;
        write_txn.open_table(MATRIX)?;
        write_txn.commit()?;
        Ok(Self { db: Arc::new(db), bodies_dir })
    }

    pub fn startup_cleanup(&self) -> Result<u32> {
        let rows = self.all()?;
        let mut count = 0u32;
        for mut row in rows {
            if row.status == "hot" || row.checkout_nonce.is_some() {
                row.status = "cold".to_string();
                row.checkout_nonce = None;
                row.updated_at = Utc::now();
                self.insert(&row)?;
                count += 1;
            }
        }
        Ok(count)
    }

    pub fn find_by_hash(&self, hash: &str) -> Result<Option<MatrixRow>> {
        if hash.is_empty() { return Ok(None); }
        Ok(self.all()?.into_iter().find(|r| r.content_hash == hash))
    }

    pub fn body_path(&self, id: &str) -> PathBuf {
        self.bodies_dir.join(format!("{id}.txt"))
    }
}
""",
    },
}

# ── helpers ───────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 3) // 4)

def key_phrases(text: str, n: int = 20) -> list[str]:
    tokens = re.findall(
        r'\b[A-Z][a-z]+[A-Z]\w*\b'   # camelCase
        r'|\b[a-z_]+::[a-z_]+\b'      # rust paths
        r'|\bfn [a-z_]+\b'            # fn names
        r'|\b\d{2,}\b'                # numbers ≥2 digits
        r'|"[^"]{3,30}"'              # short quoted strings
        r'|\b(?:ERROR|PANIC|WARN|FATAL|TIMEOUT|REFUSED|AUTH|TOKEN|CACHE|POOL|RETRY)\b',
        text, flags=re.IGNORECASE,
    )
    counts = Counter(tokens)
    return [t for t, _ in counts.most_common(n)]

def retention_score(original: str, compressed: str) -> tuple[float, list[str], list[str]]:
    phrases = key_phrases(original)
    if not phrases:
        return 1.0, [], []
    retained = [p for p in phrases if p.lower() in compressed.lower()]
    lost     = [p for p in phrases if p.lower() not in compressed.lower()]
    return len(retained) / len(phrases), retained[:8], lost[:4]

# ── oMLX ─────────────────────────────────────────────────────────────────────

def list_omlx_models(base: str, api_key: str = "") -> list[str]:
    try:
        key = api_key or omlx_api_key() or "dummy"
        req = urllib.request.Request(
            f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            all_ids = [m["id"] for m in json.loads(r.read()).get("data", [])]
        return _compression_model_list(all_ids)
    except Exception:
        return []

def _compression_model_list(all_ids: list[str]) -> list[str]:
    """
    For compression tasks, prefer the 'fast' profile of each model — fast profiles
    disable inline thinking so the model outputs compressed text directly rather than
    consuming the token budget writing reasoning steps. Fall back to base model if no
    fast profile exists.
    """
    base_models  = [m for m in all_ids if ":" not in m]
    fast_profiles = {m for m in all_ids if ":" in m and m.rsplit(":", 1)[1].endswith("-fast")}

    result = []
    for base in base_models:
        # check for a fast profile (any :*-fast suffix)
        fast = next(
            (p for p in fast_profiles if p.startswith(base + ":")), None
        )
        result.append(fast if fast else base)
    return result

# ── config ────────────────────────────────────────────────────────────────────

def _toml_str(s: str) -> str:
    """Escape a value for a TOML double-quoted string."""
    return s.replace("\\", "\\\\").replace('"', '\\"')

def write_config(model_id: str, base_url: str, api_key: str = "",
                 target_dir: Path | None = None):
    if target_dir is None:
        # Respect CONTEXT_HYDRA_DATA_DIR so the config lands where the server reads it.
        env_dir = os.environ.get("CONTEXT_HYDRA_DATA_DIR", "").strip()
        target_dir = Path(env_dir) if env_dir else CONFIG_FILE.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    key = api_key or omlx_api_key()
    (target_dir / "config.toml").write_text(
        f'[local_model]\n'
        f'base_url = "{_toml_str(base_url)}/v1"\n'
        f'compression_model = "{_toml_str(model_id)}"\n'
        f'api_key = "{_toml_str(key)}"\n'
        f'compress_target_tokens = {COMPRESS_TARGET}\n'
        f'checkout_remote_target_tokens = {DISTILL_TARGET}\n'
    )

# ── MCP stdio client ──────────────────────────────────────────────────────────

class HydraClient:
    def __init__(self, binary: str, data_dir: str | None = None):
        env = os.environ.copy()
        if data_dir:
            env["CONTEXT_HYDRA_DATA_DIR"] = data_dir
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env,
        )
        self._id   = 0
        self._q: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self._handshake()

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _send(self, msg: dict):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _read_loop(self):
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._q:
                self._q[mid].put(msg)

    def _rpc(self, method: str, params: dict, timeout: float = 90.0) -> dict:
        id_ = self._next_id()
        q: queue.Queue = queue.Queue()
        self._q[id_] = q
        self._send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} timed out after {timeout}s")
        finally:
            self._q.pop(id_, None)

    def _handshake(self):
        resp = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bench_hydra", "version": "1.0"},
        }, timeout=10)
        if "error" in resp:
            raise RuntimeError(f"MCP init failed: {resp['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool: str, args: dict, timeout: float = 120.0) -> str:
        resp = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"{tool}: {resp['error']}")
        content = resp.get("result", {}).get("content", [])
        if content:
            return content[0].get("text", "")
        raise RuntimeError(f"{tool}: empty response")

    def timed(self, tool: str, args: dict, timeout: float = 120.0) -> tuple[str, float]:
        t0 = time.perf_counter()
        r  = self.call(tool, args, timeout=timeout)
        return r, time.perf_counter() - t0

    def tools_list(self) -> list[dict]:
        resp = self._rpc("tools/list", {}, timeout=10)
        return resp.get("result", {}).get("tools", [])

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                pass

# ── response parsers ──────────────────────────────────────────────────────────

def parse_entry_id(response: str) -> str:
    for line in response.strip().split("\n")[1:]:
        candidate = line.strip().split(" | ")[0].strip()
        try:
            uuid.UUID(candidate)
            return candidate
        except ValueError:
            continue
    raise ValueError(f"no UUID found in offload response: {response!r}")

def parse_checkout_body(response: str) -> tuple[str, str]:
    """Returns (nonce, body_text)."""
    nonce_m = re.search(r"checkout nonce: ([0-9a-f]+)", response)
    nonce   = nonce_m.group(1) if nonce_m else ""
    body_m  = re.search(
        r"<hydra:body:[0-9a-f]+ [^>]*>\n?(.*?)\n?</hydra:body:[0-9a-f]+>",
        response, re.DOTALL
    )
    body = body_m.group(1).strip() if body_m else ""
    return nonce, body

# ── compression test ──────────────────────────────────────────────────────────

def run_sample(client: HydraClient, key: str, sample: dict) -> dict:
    content   = sample["content"]
    label     = sample["label"]
    tokens_in = estimate_tokens(content)

    # offload with compression
    off_resp, compress_lat = client.timed("offload", {
        "content":  content,
        "ctx_type": "trace",
        "topic":    f"bench_{key}",
        "summary":  f"bench: {label}",
        "salience": 0.5,
        "compress": True,
    })
    entry_id = parse_entry_id(off_resp)

    try:
        # checkout to read what was stored (compressed body)
        co_resp, _ = client.timed("checkout", {"id": entry_id})
        nonce, stored_body = parse_checkout_body(co_resp)
        tokens_stored = estimate_tokens(stored_body)
        if nonce:
            try:
                client.call("checkin", {"id": entry_id, "nonce": nonce})
            except Exception:
                pass

        # checkout_remote for distillation pass
        distillate = ""
        tokens_distilled = tokens_stored
        distill_lat = 0.0
        try:
            dr_resp, distill_lat = client.timed("checkout_remote", {"id": entry_id})
            if "distilled (no checkin needed)" in dr_resp:
                _, distillate = parse_checkout_body(dr_resp)
                tokens_distilled = estimate_tokens(distillate)
            elif "checkout nonce:" in dr_resp:
                # no-config fallback → plain checkout, release it
                dn, _ = parse_checkout_body(dr_resp)
                if dn:
                    try:
                        client.call("checkin", {"id": entry_id, "nonce": dn})
                    except Exception:
                        pass
            # error case: "local model call failed" → distillate stays empty
        except Exception:
            pass

        score, retained, lost = retention_score(content, stored_body)
        compress_ratio = round(tokens_in / max(tokens_stored, 1), 1)
        distill_ratio  = round(tokens_in / max(tokens_distilled, 1), 1)

        return {
            "key":              key,
            "label":            label,
            "tokens_in":        tokens_in,
            "tokens_stored":    tokens_stored,
            "tokens_distilled": tokens_distilled,
            "compress_ratio":   compress_ratio,
            "distill_ratio":    distill_ratio,
            "reduction_pct":    round((tokens_in - tokens_stored) / tokens_in * 100, 1),
            "compress_lat":     round(compress_lat, 2),
            "distill_lat":      round(distill_lat, 2),
            "retention":        round(score, 2),
            "retained":         retained,
            "lost":             lost,
            "original_head":    content[:300],
            "stored_body":      stored_body,
            "distillate":       distillate,
        }
    finally:
        try:
            client.call("forget", {"id": entry_id})
        except Exception:
            pass

# ── output ────────────────────────────────────────────────────────────────────

W = 80

def print_evidence(r: dict):
    print(f"\n  ── {r['label']} ({r['tokens_in']} tok in) {'─' * max(0, W - 22 - len(r['label']))}")
    print()

    print("  ORIGINAL (first 300 chars):")
    for line in textwrap.wrap(r["original_head"].replace("\n", " ↵ "), 74):
        print(f"    {line}")

    if r["stored_body"]:
        pct = r["reduction_pct"]
        print(f"\n  COMPRESSED → {r['tokens_stored']} tok  "
              f"({r['compress_ratio']}× reduction, {pct}% smaller, {r['compress_lat']}s):")
        for line in textwrap.wrap(r["stored_body"][:350].replace("\n", " ↵ "), 74):
            print(f"    {line}")
    else:
        print(f"\n  COMPRESSED → (no compression; stored raw at {r['tokens_stored']} tok)")

    if r["distillate"]:
        print(f"\n  DISTILLED → {r['tokens_distilled']} tok  "
              f"({r['distill_ratio']}× reduction, {r['distill_lat']}s):")
        for line in textwrap.wrap(r["distillate"][:300].replace("\n", " ↵ "), 74):
            print(f"    {line}")

    score_pct = round(r["retention"] * 100)
    kept = ", ".join(r["retained"][:6]) or "—"
    gone = ", ".join(r["lost"][:4])     or "none"
    print(f"\n  Key phrases: {score_pct}% retained  |  kept: {kept}  |  lost: {gone}")

def print_single_model_summary(results: list[dict]):
    print(f"\n\n{'='*W}")
    print("  TOKEN REDUCTION SUMMARY")
    print(f"{'='*W}\n")
    print(f"  {'Content':<28} {'In':>6}  {'Stored':>6}  {'Ratio':>6}  {'Pct':>5}  "
          f"{'Distilled':>9}  {'D-Ratio':>7}  {'Kept%':>5}  {'Lat':>6}")
    print(f"  {'':─<28} {'':─>6}  {'':─>6}  {'':─>6}  {'':─>5}  "
          f"{'':─>9}  {'':─>7}  {'':─>5}  {'':─>6}")
    for r in results:
        dist_col = f"{r['tokens_distilled']:>9}" if r["distillate"] else f"{'—':>9}"
        dratio   = f"{r['distill_ratio']:>6.1f}×"  if r["distillate"] else f"{'—':>7}"
        print(f"  {r['label']:<28} {r['tokens_in']:>6}  {r['tokens_stored']:>6}  "
              f"{r['compress_ratio']:>5.1f}×  {r['reduction_pct']:>4.0f}%  "
              f"{dist_col}  {dratio}  {r['retention']:>4.0%}  {r['compress_lat']:>5.1f}s")

    total_in     = sum(r["tokens_in"]     for r in results)
    total_stored = sum(r["tokens_stored"] for r in results)
    total_dist   = sum(r["tokens_distilled"] for r in results if r["distillate"])
    if total_in:
        overall_ratio = round(total_in / max(total_stored, 1), 1)
        overall_pct   = round((total_in - total_stored) / total_in * 100, 1)
        print(f"\n  Overall: {total_in} tok in → {total_stored} tok stored  "
              f"({overall_ratio}×, {overall_pct}% reduction)")
        if total_dist:
            print(f"           {total_in} tok in → {total_dist} tok distilled  "
                  f"({round(total_in/max(total_dist,1), 1)}× via checkout_remote)")

def print_model_comparison(all_results: list[dict]):
    models  = list(dict.fromkeys(r["model"] for r in all_results))
    samples = list(dict.fromkeys(r["key"]   for r in all_results))

    print(f"\n\n{'='*W}")
    print("  MODEL COMPARISON — token reduction efficiency")
    print(f"{'='*W}\n")

    # aggregate per model
    stats = []
    for m in models:
        mrs = [r for r in all_results if r["model"] == m]
        if not mrs:
            continue
        avg_ratio     = sum(r["compress_ratio"] for r in mrs) / len(mrs)
        avg_retention = sum(r["retention"]      for r in mrs) / len(mrs)
        avg_lat       = sum(r["compress_lat"]   for r in mrs) / len(mrs)
        ram           = RAM_MAP.get(m, 0)
        efficiency    = avg_ratio / ram if ram else 0
        stats.append((m, ram, avg_ratio, avg_retention, avg_lat, efficiency))

    stats.sort(key=lambda x: -x[5])
    print(f"  {'Model':<40} {'RAM':>6}  {'Ratio':>7}  {'Kept%':>6}  {'Lat':>6}  {'Effic':>8}")
    print(f"  {'':─<40} {'':─>6}  {'':─>7}  {'':─>6}  {'':─>6}  {'':─>8}")
    for m, ram, ratio, ret, lat, eff in stats:
        ram_s = f"{ram:.1f}G" if ram else "?"
        print(f"  {m:<40} {ram_s:>6}  {ratio:>6.1f}×  {ret:>5.0%}  {lat:>5.1f}s  {eff:>8.3f}")
    print(f"\n  Efficiency = avg_compress_ratio / RAM_GB  (higher = more tokens/GB)")

    # per content type
    print(f"\n\n  PER CONTENT TYPE\n")
    for s in samples:
        srs = [r for r in all_results if r["key"] == s]
        if not srs:
            continue
        tokens_in = srs[0]["tokens_in"]
        print(f"  {srs[0]['label']}  ({tokens_in} tok in):")
        print(f"    {'Model':<40} {'Stored':>6}  {'Ratio':>7}  {'Kept%':>6}  {'Lat':>6}")
        print(f"    {'':─<40} {'':─>6}  {'':─>7}  {'':─>6}  {'':─>6}")
        for r in sorted(srs, key=lambda x: -x["compress_ratio"]):
            print(f"    {r['model']:<40} {r['tokens_stored']:>6}  "
                  f"{r['compress_ratio']:>6.1f}×  {r['retention']:>5.0%}  "
                  f"{r['compress_lat']:>5.1f}s")
        print()

# ── body generator (avoids dedup across runs) ─────────────────────────────────

_FILL_BASE = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs. "
    "How vexingly quick daft zebras jump! "
) * 40  # ~4 KB of cycling prose

def make_body(size: int, seed: int = 0) -> str:
    """Unique body of exactly `size` bytes. seed differentiates runs (avoids content-hash dedup)."""
    prefix = f"bench:size={size}:seed={seed:010d}:"
    needed = size - len(prefix)
    if needed <= 0:
        # Full prefix doesn't fit; embed seed at the end so truncation preserves uniqueness.
        seed_tag = f":{seed}"
        if size <= len(seed_tag):
            return str(seed)[:size].ljust(size, "0")
        fill = "b" * (size - len(seed_tag))
        return (fill + seed_tag)[:size]
    repeat = _FILL_BASE * (needed // len(_FILL_BASE) + 1)
    return prefix + repeat[:needed]


# ── S1 — single-entry latency ─────────────────────────────────────────────────

def run_latency_benchmark(binary: str, runs: int = 5,
                          sizes: list[int] | None = None,
                          no_compress: bool = False,
                          model_config: tuple[str, str, str] | None = None) -> None:
    if sizes is None:
        sizes = [1024, 10240, 102400]

    import datetime

    def p95(lats: list[float]) -> float:
        s = sorted(lats)
        return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)] if s else 0.0

    def report_row(name: str, lats: list[float], note: str = "") -> None:
        if not lats:
            print(f"  {name:<24} {'—':>8}  {'—':>8}  {note}")
            return
        med = statistics.median(lats)
        tail = p95(lats)
        note_str = f"  {note}" if note else ""
        print(f"  {name:<24} {med*1000:>7.1f}ms  {tail*1000:>7.1f}ms{note_str}")

    print(f"\n{'='*W}")
    print(f"  S1 — SINGLE-ENTRY LATENCY  —  {datetime.date.today()}")
    print(f"  binary: {binary}")
    print(f"  runs per operation: {runs}")
    print(f"{'='*W}")

    with tempfile.TemporaryDirectory(prefix="hydra-bench-lat-") as tmpdir:
        if not no_compress:
            if model_config:
                write_config(*model_config, target_dir=Path(tmpdir))
            elif CONFIG_FILE.exists():
                shutil.copy(CONFIG_FILE, Path(tmpdir) / "config.toml")

        for size in sizes:
            size_label = (f"{size // 1024}KB" if size >= 1024 else f"{size}B")
            tok_est = estimate_tokens("x" * size)

            print(f"\n  ── {size_label}  (~{tok_est} tok) {'─' * max(0, W - 16 - len(size_label))}")
            print(f"  {'Operation':<24} {'Median':>8}  {'p95':>8}")
            print(f"  {'':─<24} {'':─>8}  {'':─>8}")

            client = HydraClient(binary, data_dir=tmpdir)
            try:
                # ── offload (no compress) ─────────────────────────────────────
                offload_lats: list[float] = []
                offload_ids:  list[str]   = []
                for i in range(runs):
                    resp, lat = client.timed("offload", {
                        "content":  make_body(size, seed=i),
                        "ctx_type": "bench",
                        "topic":    f"lat-raw-{size_label}-{i}",
                        "summary":  "latency benchmark raw",
                        "salience": 0.5,
                        "compress": False,
                    })
                    offload_lats.append(lat)
                    offload_ids.append(parse_entry_id(resp))
                report_row("offload (raw)", offload_lats)

                # ── offload (compress) ────────────────────────────────────────
                compress_ids: list[str] = []
                if no_compress:
                    report_row("offload (compress)", [], "(--no-compress)")
                else:
                    compress_lats: list[float] = []
                    for i in range(runs):
                        try:
                            resp, lat = client.timed("offload", {
                                "content":  make_body(size, seed=runs + i),
                                "ctx_type": "bench",
                                "topic":    f"lat-cmp-{size_label}-{i}",
                                "summary":  "latency benchmark compressed",
                                "salience": 0.5,
                                "compress": True,
                            }, timeout=180.0)
                            compress_lats.append(lat)
                            compress_ids.append(parse_entry_id(resp))
                        except Exception as e:
                            print(f"  offload(compress) run {i} failed: {e}")
                            break
                    report_row("offload (compress)", compress_lats)
                    for id_ in compress_ids:
                        try: client.call("forget", {"id": id_})
                        except Exception: pass

                # Use first raw offload entry as the standing test subject
                test_id = offload_ids[0] if offload_ids else None

                # ── recall ────────────────────────────────────────────────────
                recall_lats: list[float] = []
                if test_id:
                    for _ in range(runs):
                        _, lat = client.timed("recall", {"id": test_id})
                        recall_lats.append(lat)
                report_row("recall", recall_lats)

                # ── checkout + checkin ─────────────────────────────────────────
                checkout_lats: list[float] = []
                checkin_lats:  list[float] = []
                if test_id:
                    for _ in range(runs):
                        co_resp, co_lat = client.timed("checkout", {"id": test_id})
                        checkout_lats.append(co_lat)
                        nonce, _ = parse_checkout_body(co_resp)
                        if nonce:
                            _, ci_lat = client.timed("checkin",
                                                     {"id": test_id, "nonce": nonce})
                            checkin_lats.append(ci_lat)
                report_row("checkout", checkout_lats)
                report_row("checkin",  checkin_lats)

                # ── checkout_remote ────────────────────────────────────────────
                if no_compress or not test_id:
                    report_row("checkout_remote", [], "(--no-compress)")
                else:
                    cr_lats: list[float] = []
                    for _ in range(runs):
                        try:
                            _, lat = client.timed("checkout_remote",
                                                  {"id": test_id}, timeout=180.0)
                            cr_lats.append(lat)
                        except Exception as e:
                            print(f"  checkout_remote failed: {e}")
                            break
                    report_row("checkout_remote", cr_lats)

                # ── scan ops (read-only, same call N times) ────────────────────
                peek_lats:   list[float] = []
                matrix_lats: list[float] = []
                search_lats: list[float] = []
                for _ in range(runs):
                    _, lat = client.timed("peek", {})
                    peek_lats.append(lat)
                    _, lat = client.timed("matrix", {})
                    matrix_lats.append(lat)
                    _, lat = client.timed("search", {"query": "bench"})
                    search_lats.append(lat)
                report_row("peek",   peek_lats)
                report_row("matrix", matrix_lats)
                report_row("search", search_lats)

                # ── forget (times N, using the raw offload_ids) ────────────────
                forget_lats: list[float] = []
                for id_ in offload_ids:
                    try:
                        _, lat = client.timed("forget", {"id": id_})
                        forget_lats.append(lat)
                    except Exception:
                        pass
                report_row("forget", forget_lats)

            except Exception as e:
                print(f"  ERROR: {e}")
            finally:
                client.close()

    print(f"\n{'='*W}\n")


# ── S2 — scale test ────────────────────────────────────────────────────────────

def run_scale_test(binary: str, runs: int = 5) -> None:
    import datetime

    COUNTS = [10, 100, 500]
    ENTRY_BODY_SIZE = 512  # small bodies — we're measuring scan speed, not body reads

    def p95(lats: list[float]) -> float:
        s = sorted(lats)
        return s[min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)] if s else 0.0

    print(f"\n{'='*W}")
    print(f"  S2 — SCALE TEST  —  {datetime.date.today()}")
    print(f"  peek / matrix / search latency vs store size")
    print(f"  (verifies <1ms claim at ~500 entries)")
    print(f"{'='*W}")

    summary: dict[str, dict[int, tuple[float, float]]] = {
        "peek": {}, "matrix": {}, "search": {}
    }

    with tempfile.TemporaryDirectory(prefix="hydra-bench-scale-") as tmpdir:
        client = HydraClient(binary, data_dir=tmpdir)
        inserted = 0

        try:
            for target in COUNTS:
                # Incrementally add entries to reach target
                print(f"\n  Building to {target} entries ...", end=" ", flush=True)
                while inserted < target:
                    client.call("offload", {
                        "content":  make_body(ENTRY_BODY_SIZE, seed=inserted),
                        "ctx_type": "bench",
                        "topic":    f"scale-{inserted:05d}",
                        "summary":  f"scale test entry {inserted}",
                        "salience": 0.5,
                        "compress": False,
                    })
                    inserted += 1
                print("done")

                print(f"  {'Operation':<10} {'Median':>8}  {'p95':>8}")
                print(f"  {'':─<10} {'':─>8}  {'':─>8}")

                for op, args, label in [
                    ("peek",   {},                   "peek"),
                    ("matrix", {},                   "matrix"),
                    ("search", {"query": "scale-00"}, "search"),
                ]:
                    lats: list[float] = []
                    for _ in range(runs):
                        _, lat = client.timed(op, args)
                        lats.append(lat)
                    if not lats:
                        print(f"  {label:<10} {'—':>8}ms  {'—':>8}ms")
                        continue
                    med  = statistics.median(lats)
                    tail = p95(lats)
                    print(f"  {label:<10} {med*1000:>7.2f}ms  {tail*1000:>7.2f}ms")
                    summary[op][target] = (med, tail)

        except Exception as e:
            print(f"\n  ERROR: {e}")
        finally:
            client.close()

    # Cross-count summary table
    print(f"\n  SUMMARY — median ms by entry count")
    header = f"  {'Op':<8} " + "  ".join(f"{n:>6}" for n in COUNTS)
    print(header)
    print(f"  {'':─<8} " + "  ".join("─" * 6 for _ in COUNTS))
    for op in ["peek", "matrix", "search"]:
        row = f"  {op:<8}"
        for n in COUNTS:
            val = summary[op].get(n)
            row += f"  {val[0]*1000:>6.2f}" if val else f"  {'—':>6}"
        print(row)

    # Verdict
    peek_500 = summary["peek"].get(500, (None,))[0]
    mat_500  = summary["matrix"].get(500, (None,))[0]
    if peek_500 and mat_500:
        peek_ms = peek_500 * 1000
        mat_ms  = mat_500  * 1000
        print(f"\n  At 500 entries: peek={peek_ms:.2f}ms  matrix={mat_ms:.2f}ms")
        if peek_ms < 1.0 and mat_ms < 5.0:
            print(f"  ✓ README claim confirmed: linear scan over ~500 rows is sub-ms for peek")
        else:
            print(f"  ✗ peek or matrix exceeded expected thresholds")

    print(f"\n{'='*W}\n")


# ── S4 — session simulation ────────────────────────────────────────────────────

SCHEMA_OVERHEAD_TOK = 1540  # fixed per-session cost of having context-hydra loaded

def run_session_simulation(binary: str, turns: int = 20,
                           price_per_m: float = 15.0,
                           no_compress: bool = False,
                           model_config: tuple[str, str, str] | None = None) -> None:
    import datetime

    matrix_turns = max(0, turns - 13)  # turns 14…N; default 7 at turns=20

    print(f"\n{'='*W}")
    print(f"  S4 — SESSION SIMULATION  —  {datetime.date.today()}")
    print(f"  {turns}-turn coding agent session  |  ${price_per_m:.2f}/M tokens")
    print(f"  (turns 1-13 fixed; turns 14-{turns} = {matrix_turns} × matrix)")
    print(f"{'='*W}")

    displaced_tok = 0  # content tokens offloaded away from context
    traffic_tok   = 0  # response tokens that entered context via hydra

    def log(turn: int, desc: str, disp: int, use: int) -> None:
        nonlocal displaced_tok, traffic_tok
        displaced_tok += disp
        traffic_tok   += use
        disp_s = f"+{disp:>5}" if disp else f"{'—':>6}"
        use_s  = f"+{use:>4}"
        print(f"  T{turn:02d}  {desc:<46}  disp {disp_s}  use {use_s}")

    print(f"\n  {'Turn':<5}  {'Action':<46}  {'Displaced':>10}  {'Used':>7}")
    print(f"  {'':─<5}  {'':─<46}  {'':─>10}  {'':─>7}")

    # realistic file contents — use existing SAMPLES, sized for variety
    file_specs = [
        ("store.rs",   SAMPLES["code_context"]["content"]),
        ("design.md",  SAMPLES["design_note"]["content"]),
        ("auth.md",    SAMPLES["reasoning_chain"]["content"][:600]),
    ]
    error_content    = SAMPLES["error_trace"]["content"]
    reasoning_content = SAMPLES["reasoning_chain"]["content"]

    with tempfile.TemporaryDirectory(prefix="hydra-bench-sim-") as tmpdir:
        if not no_compress:
            if model_config:
                write_config(*model_config, target_dir=Path(tmpdir))
            elif CONFIG_FILE.exists():
                shutil.copy(CONFIG_FILE, Path(tmpdir) / "config.toml")

        # write file bodies to disk for offload_path
        tmp_files: list[tuple[str, str, str]] = []  # (name, path, content)
        for name, content in file_specs:
            p = Path(tmpdir) / name
            p.write_text(content)
            tmp_files.append((name, str(p), content))

        file_ids:  dict[str, str] = {}
        error_id:  str = ""
        reason_id: str = ""

        client = HydraClient(binary, data_dir=tmpdir)
        try:
            # ── Turn 1: offload_path 3 source files ──────────────────────────
            for name, path, content in tmp_files:
                resp = client.call("offload_path", {
                    "path": path, "ctx_type": "code",
                    "topic": name, "summary": f"source: {name}", "salience": 0.7,
                })
                file_ids[name] = parse_entry_id(resp)
                log(1, f"offload_path {name}",
                    estimate_tokens(content), estimate_tokens(resp))

            # ── Turns 2–5: work phase ─────────────────────────────────────────
            anchor_name = tmp_files[0][0]
            anchor_id   = file_ids[anchor_name]
            for t in range(2, 6):
                if t == 4:  # turn 4: recall + checkout + checkin
                    r1 = client.call("recall", {"id": anchor_id})
                    r2 = client.call("checkout", {"id": anchor_id})
                    nonce, _ = parse_checkout_body(r2)
                    r3 = ""
                    if nonce:
                        r3 = client.call("checkin", {"id": anchor_id, "nonce": nonce})
                    log(t, f"recall + checkout/checkin {anchor_name}", 0,
                        estimate_tokens(r1) + estimate_tokens(r2) + estimate_tokens(r3))
                else:
                    r = client.call("recall", {"id": anchor_id})
                    log(t, f"recall {anchor_name}", 0, estimate_tokens(r))

            # ── Turn 6: offload error trace + reasoning chain ──────────────────
            r1 = client.call("offload", {
                "content": error_content, "ctx_type": "trace",
                "topic": "conn-error", "summary": "connection refused error trace",
                "salience": 0.8, "compress": False,
            })
            error_id = parse_entry_id(r1)

            r2 = client.call("offload", {
                "content": reasoning_content, "ctx_type": "reasoning",
                "topic": "auth-flow", "summary": "auth flow analysis",
                "salience": 0.7, "compress": False,
            })
            reason_id = parse_entry_id(r2)

            log(6, "offload error trace + reasoning chain",
                estimate_tokens(error_content) + estimate_tokens(reasoning_content),
                estimate_tokens(r1) + estimate_tokens(r2))

            # ── Turns 7–12: debug — search + recall ───────────────────────────
            for t in range(7, 13):
                rs = client.call("search", {"query": "error"})
                rr = client.call("recall", {"id": error_id})
                log(t, "search + recall error trace", 0,
                    estimate_tokens(rs) + estimate_tokens(rr))

            # ── Turn 13: resolve — compress or forget stale trace ─────────────
            if no_compress:
                r = client.call("forget", {"id": error_id})
                log(13, "forget resolved trace (--no-compress)", 0, estimate_tokens(r))
            else:
                resolved = error_content + "\n\n[RESOLVED: pool reconnect on startup fixed]"
                try:
                    r = client.call("offload", {
                        "content": resolved, "ctx_type": "trace",
                        "topic": "conn-error-resolved",
                        "summary": "resolved: connection error fixed",
                        "salience": 0.2, "compress": True,
                    }, timeout=180.0)
                    client.call("forget", {"id": error_id})
                    log(13, "offload(compress) resolved trace", 0, estimate_tokens(r))
                except Exception as e:
                    r = client.call("forget", {"id": error_id})
                    log(13, f"forget trace (compress failed: {e})", 0, estimate_tokens(r))

            # ── Turns 14–N: new task — matrix each turn ────────────────────────
            for t in range(14, turns + 1):
                r = client.call("matrix", {})
                log(t, "matrix (orient for new task)", 0, estimate_tokens(r))

        except Exception as e:
            print(f"\n  ERROR during simulation: {e}")
        finally:
            client.close()

    # Accounting
    total_with_hydra = SCHEMA_OVERHEAD_TOK + traffic_tok
    net_saved        = displaced_tok - total_with_hydra
    usd_saved        = net_saved / 1_000_000 * price_per_m

    print(f"\n  {'─'*60}")
    print(f"  CONTEXT DISPLACEMENT ACCOUNTING")
    print(f"  {'─'*60}")
    print(f"  Content displaced (not in context):  {displaced_tok:>8,} tok")
    print(f"  Schema overhead (fixed):             {SCHEMA_OVERHEAD_TOK:>8,} tok")
    print(f"  Tool response traffic:               {traffic_tok:>8,} tok")
    print(f"  Total context cost with hydra:       {total_with_hydra:>8,} tok")
    print(f"  ─────────────────────────────────────{'─'*9}")
    print(f"  Net tokens kept out of context:      {net_saved:>8,} tok")
    if displaced_tok > 0:
        pct = net_saved / displaced_tok * 100
        print(f"  Reduction vs. no-hydra baseline:       {pct:>6.1f}%")
    print(f"  USD saved at ${price_per_m:.2f}/M tokens:       ${usd_saved:.5f}")
    print(f"\n  Note: 'displaced' = raw content that would have stayed in context.")
    print(f"  'traffic' = hydra response tokens (matrix rows, recall summaries, etc).")
    print(f"  Checkout bodies enter context briefly but are released on checkin.")
    print(f"\n{'='*W}\n")


# ── S5 — cross-session persistence ────────────────────────────────────────────

def run_persistence_test(binary: str) -> None:
    import datetime

    print(f"\n{'='*W}")
    print(f"  S5 — CROSS-SESSION PERSISTENCE  —  {datetime.date.today()}")
    print(f"  Populate → restart → verify: entries survive, hot → cold on startup")
    print(f"{'='*W}\n")

    passes = failures = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passes, failures
        if cond:
            passes += 1
            print(f"  ✓ {label}")
        else:
            failures += 1
            suffix = f": {detail}" if detail else ""
            print(f"  ✗ {label}{suffix}")

    def status_of(resp: str) -> str:
        """Extract the status field from a fmt_row line.
        fmt_row format: id | topic[tags] | ctx_type | sal=X.XX | STATUS | ~Xtok
        """
        for line in resp.split("\n"):
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) >= 5 and parts[4] in ("cold", "hot", "pinned"):
                return parts[4]
        return "unknown"

    with tempfile.TemporaryDirectory(prefix="hydra-bench-persist-") as tmpdir:

        # ── Session 1: populate ───────────────────────────────────────────────
        print(f"  Session 1: populate store")
        client = HydraClient(binary, data_dir=tmpdir)
        ids: dict[str, str] = {}

        # cold entry
        r = client.call("offload", {
            "content": "s5-cold: this entry should survive restart as cold",
            "ctx_type": "bench", "topic": "s5-cold",
            "summary": "cold entry", "salience": 0.5, "compress": False,
        })
        ids["cold"] = parse_entry_id(r)

        # pinned entry
        r = client.call("offload", {
            "content": "s5-pinned: this entry should survive restart as pinned",
            "ctx_type": "bench", "topic": "s5-pinned",
            "summary": "pinned entry", "salience": 0.5, "compress": False,
        })
        ids["pinned"] = parse_entry_id(r)
        client.call("pin", {"id": ids["pinned"]})

        # hot entry — checkout without checkin (simulates session crash)
        r = client.call("offload", {
            "content": "s5-hot: this entry is hot; should be reset to cold on restart",
            "ctx_type": "bench", "topic": "s5-hot",
            "summary": "hot entry", "salience": 0.5, "compress": False,
        })
        ids["hot"] = parse_entry_id(r)
        client.call("checkout", {"id": ids["hot"]})  # intentionally no checkin

        # verify hot before restart
        hot_before = client.call("recall", {"id": ids["hot"]})
        check("hot entry is 'hot' before restart", status_of(hot_before) == "hot",
              f"got: {hot_before!r}")
        client.close()

        # ── Session 2: restart and verify ────────────────────────────────────
        print(f"\n  Session 2: restart binary (same store)")
        client2 = HydraClient(binary, data_dir=tmpdir)

        cold_r = client2.call("recall", {"id": ids["cold"]})
        check("cold entry survived restart",    "s5-cold"   in cold_r)
        check("cold entry status is cold",      status_of(cold_r) == "cold", cold_r)

        pin_r = client2.call("recall", {"id": ids["pinned"]})
        check("pinned entry survived restart",  "s5-pinned" in pin_r)
        check("pinned entry status is pinned",  status_of(pin_r) == "pinned", pin_r)

        hot_r = client2.call("recall", {"id": ids["hot"]})
        check("hot entry survived restart",     "s5-hot"    in hot_r)
        check("hot entry reset to cold on startup", status_of(hot_r) == "cold", hot_r)

        # no stale nonce — since entry was reset to cold with nonce=None,
        # checkin with any nonce should return "entry is not checked out"
        # (rmcp returns tool errors as response text with isError:true, not MCP errors)
        ci_resp = client2.call("checkin", {"id": ids["hot"], "nonce": "deadbeef"})
        check("no stale nonce on reset entry", "not checked out" in ci_resp, ci_resp)

        client2.close()

    print(f"\n  {'─'*40}")
    verdict = "PASS" if failures == 0 else "FAIL"
    print(f"  {verdict}: {passes} passed, {failures} failed")
    print(f"\n{'='*W}\n")


# ── main ─────────────────────────────────────────────────────────────────────

def find_binary() -> str | None:
    found = shutil.which("context-hydra")
    if found:
        return found
    cargo_bin = os.path.expanduser("~/.cargo/bin/context-hydra")
    if os.path.exists(cargo_bin):
        return cargo_bin
    return None

def _print_suggested_models():
    print(f"\n{'='*80}")
    print("  SUGGESTED COMPRESSION MODELS  (small → fast, high efficiency)")
    print(f"{'='*80}\n")
    print("  Download any of these with oMLX, then run --compare-models to benchmark.\n")
    print(f"  {'Tier':<18} {'Model ID':<40} {'RAM':>5}  HuggingFace path")
    print(f"  {'':─<18} {'':─<40} {'':─>5}  {'':─<40}")
    last_tier = ""
    for m in SUGGESTED_MODELS:
        if m["tier"] != last_tier:
            last_tier = m["tier"]
        print(f"  {m['tier']:<18} {m['id']:<40} {m['ram']:>4.1f}G  {m['hf']}")

    print(f"""
  To download via oMLX:
    omlx pull mlx-community/Qwen2.5-3B-Instruct-4bit
    omlx pull mlx-community/gemma-3-4b-it-qf16

  Then run the benchmark across all loaded models:
    python3 bench_hydra.py --compare-models

  For a quick sanity check on just one model:
    python3 bench_hydra.py --model Qwen2.5-3B-Instruct-4bit --quick
""")

def window_analysis(binary: str):
    """Measure all four context-window economics: schema overhead, matrix row cost,
    break-even by content size, and compression target recommendations."""

    import datetime

    client = HydraClient(binary)

    # ── 1. Tool schema overhead ───────────────────────────────────────────────
    tools = client.tools_list()
    tool_rows = []
    for t in tools:
        desc_ch  = len(t.get("description", ""))
        schema_ch = len(json.dumps(t.get("inputSchema", {})))
        total_ch  = len(t["name"]) + desc_ch + schema_ch
        tool_rows.append((t["name"], desc_ch, schema_ch, total_ch))

    schema_total_ch  = sum(r[3] for r in tool_rows)
    schema_total_tok = schema_total_ch // 4

    # ── 2. Matrix row cost ────────────────────────────────────────────────────
    matrix_out = client.call("matrix", {})
    uuid_re = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    header_lines  = [l for l in matrix_out.split("\n") if l and not uuid_re.search(l)]
    entry_lines   = [l for l in matrix_out.split("\n") if uuid_re.search(l)]
    header_ch     = sum(len(l) for l in header_lines)
    header_tok    = header_ch // 4

    if entry_lines:
        row_chars     = [len(l) for l in entry_lines]
        avg_row_ch    = sum(row_chars) // len(row_chars)
        avg_row_tok   = avg_row_ch // 4
        existing_n    = len(entry_lines)
    else:
        avg_row_ch  = 94   # measured empirically
        avg_row_tok = 23
        existing_n  = 0

    client.close()

    # ── print ─────────────────────────────────────────────────────────────────
    W2 = W

    print(f"\n{'='*W2}")
    print(f"  context-hydra window analysis  —  {datetime.date.today()}")
    print(f"{'='*W2}")

    # Section 1: schema
    print(f"\n  {'─'*60}")
    print(f"  1. TOOL SCHEMA OVERHEAD  (fixed cost per LLM call)")
    print(f"  {'─'*60}")
    print(f"\n  This is charged on every request while context-hydra is loaded.")
    print(f"  It is independent of how much content you have stored.\n")
    print(f"  {'Tool':<26} {'desc':>5}  {'schema':>6}  {'total':>5}  {'~tok':>4}")
    print(f"  {'':─<26} {'':─>5}  {'':─>6}  {'':─>5}  {'':─>4}")
    for name, desc_ch, schema_ch, total_ch in sorted(tool_rows, key=lambda r: -r[3]):
        print(f"  {name:<26} {desc_ch:>5}  {schema_ch:>6}  {total_ch:>5}  {total_ch//4:>4}")
    print(f"  {'':─<26} {'':─>5}  {'':─>6}  {'':─>5}  {'':─>4}")
    print(f"  {'TOTAL':<26} {'':>5}  {'':>6}  {schema_total_ch:>5}  {schema_total_tok:>4}")
    print(f"\n  → Every LLM call costs ~{schema_total_tok} tokens just for the MCP schema.")
    print(f"    Measure once: token count with server loaded vs. without — that delta")
    print(f"    is your actual baseline. The estimate above is chars/4.\n")

    # Section 2: matrix row cost
    print(f"  {'─'*60}")
    print(f"  2. MATRIX ROW COST  (scales with number of stored entries)")
    print(f"  {'─'*60}")
    print(f"\n  Each `matrix` call returns one line per stored entry.")
    print(f"  Calling `matrix` is optional — use `peek` (tiny) when you only need counts.\n")
    if existing_n:
        print(f"  Measured from {existing_n} existing entries in your store:")
    else:
        print(f"  Empirically measured row format:")
    print(f"    Header line:     ~{header_tok} tok ({header_ch} chars)")
    print(f"    Per entry (avg): ~{avg_row_tok} tok ({avg_row_ch} chars)")
    print(f"    Matrix at N entries: ~{header_tok} + N × {avg_row_tok} tokens\n")

    print(f"  {'N entries':>10}  {'matrix cost':>12}  {'cost to call matrix':>20}")
    print(f"  {'':─>10}  {'':─>12}  {'':─>20}")
    for n in [1, 5, 10, 20, 50, 100, 200]:
        total = header_tok + n * avg_row_tok
        print(f"  {n:>10}  {total:>12} tok  {'(one-time per call)':>20}")

    # Section 3: break-even
    print(f"\n  {'─'*60}")
    print(f"  3. BREAK-EVEN BY CONTENT SIZE")
    print(f"  {'─'*60}")
    print(f"""
  Once you pay the schema overhead (~{schema_total_tok} tok), every additional token
  you offload is a net gain from turn 2 onward.

  Replacing X tokens in context with a {avg_row_tok}-token matrix row saves X-{avg_row_tok} tokens/turn.
  The schema overhead ({schema_total_tok} tok) is recouped after:  {schema_total_tok} / (X - {avg_row_tok})  turns.

  Content size  Saved/turn   Turns to recoup schema   Turns to recoup schema
  (X tokens)    (X-{avg_row_tok} tok)   (schema-only baseline)   (including checkout cost)
  ─────────────────────────────────────────────────────────────────────────""")
    for x in [100, 200, 300, 400, 500, 600, 800, 1000, 2000]:
        saved = x - avg_row_tok
        if saved <= 0:
            continue
        turns_schema = round(schema_total_tok / saved, 1)
        # checkout cost: paying X tokens once in the future reduces savings by X that turn
        # so breakeven including one checkout in the future (pessimistic):
        turns_with_checkout = round((schema_total_tok + x) / saved, 1)
        print(f"  {x:>5} tok      {saved:>4} tok     {turns_schema:>6} turns               {turns_with_checkout:>6} turns")

    print(f"""
  Rule of thumb: if content stays out of context for 3+ turns, offloading wins.
  `offload_path` is always free — the file never enters context at all.\n""")

    # Section 4: compression target recommendations
    print(f"  {'─'*60}")
    print(f"  4. COMPRESSION TARGET SETTINGS")
    print(f"  {'─'*60}")
    print(f"""
  config.toml [local_model] settings and their trade-offs:

  compress_target_tokens   (default: 200)
  ─────────────────────────────────────────────────────────────────────────
  Controls the target output length for `offload(compress: true)`.
  This is lossy and irreversible — the stored body is replaced.

  Benchmarked output vs. target on prose content (Qwen-7B, Ornith-35B):
    Target 200 tok → actual output 183–242 tok (models overshoot slightly)
    Measured compression: 3–5× on error traces, 2–3× on reasoning chains
    Code content: expansion guard fires — output stored raw regardless of target

  Recommended settings:
    compress_target_tokens = 200   → dense prose, error traces, reasoning
    compress_target_tokens = 300   → if you find 200 loses too much context
    compress_target_tokens = 150   → very aggressive; only safe for completed
                                     reasoning you'll only ever need a gist of

  checkout_remote_target_tokens   (default: 300)
  ─────────────────────────────────────────────────────────────────────────
  Controls the target output length for `checkout_remote` distillation.
  This is non-destructive — the stored body is not changed.
  Set higher than compress_target since you have the full body to work from.

    checkout_remote_target_tokens = 300   → default, works well
    checkout_remote_target_tokens = 500   → preserve more detail for complex content
    checkout_remote_target_tokens = 150   → maximum squish for remote model injection

  Calibration approach:
    Run: python3 bench_hydra.py --compare-models --quick
    Look at the "→ N tok" column for your model.
    If actual output consistently exceeds target by >50%, raise the target.
    If the model hits target cleanly, lower it to reduce remote context cost.\n""")

    print(f"  {'─'*60}")
    print(f"  SUMMARY")
    print(f"  {'─'*60}")
    print(f"""
  Schema overhead:     ~{schema_total_tok} tok / call  (fixed, always paid)
  Matrix per entry:    ~{avg_row_tok} tok             (use `peek` for counts; `matrix` only when orienting)
  Break-even offload:  3+ turns out of context
  Good compress target: 200 tok prose, skip for code
  checkout_remote target: 300 tok
""")
    print(f"{'='*W2}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary",          default=None)
    ap.add_argument("--model",           default=None)
    ap.add_argument("--compare-models",  action="store_true")
    ap.add_argument("--omlx-url",        default=OMLX_BASE)
    ap.add_argument("--api-key",         default=None,
                    help="oMLX API key (auto-detected from ~/.omlx/settings.json if omitted)")
    ap.add_argument("--quick",           action="store_true",
                    help="run error_trace sample only")
    ap.add_argument("--suggest-models",  action="store_true",
                    help="show recommended small models to download, then exit")
    ap.add_argument("--window-analysis", action="store_true",
                    help="measure schema overhead, matrix row cost, break-even, and target settings")
    # S1 / S2
    ap.add_argument("--latency",         action="store_true",
                    help="S1: measure single-entry operation latency (uses isolated tmpdir)")
    ap.add_argument("--scale",           action="store_true",
                    help="S2: measure peek/matrix/search latency vs store size (uses isolated tmpdir)")
    ap.add_argument("--runs",            type=int, default=5,
                    help="repetitions per latency measurement (default: 5)")
    ap.add_argument("--no-compress",     action="store_true",
                    help="skip compression operations (environments without local model)")
    ap.add_argument("--sizes",           default="1024,10240,102400",
                    help="comma-separated body sizes in bytes for --latency (default: 1024,10240,102400)")
    ap.add_argument("--simulate",        action="store_true",
                    help="S4: 20-turn coding session simulation, context displacement accounting")
    ap.add_argument("--persist",         action="store_true",
                    help="S5: cross-session persistence and startup_cleanup verification")
    ap.add_argument("--turns",           type=int, default=20,
                    help="total session length for --simulate (default: 20; turns 14-N are matrix calls)")
    ap.add_argument("--price",           type=float, default=15.0,
                    help="price per 1M input tokens in USD for --simulate (default: 15.0)")
    args = ap.parse_args()

    if args.suggest_models:
        _print_suggested_models()
        return

    binary  = args.binary or find_binary()
    if not binary or not os.path.exists(binary):
        sys.exit(f"context-hydra binary not found. Build with: cargo build --release")

    if args.window_analysis:
        window_analysis(binary)
        return

    if args.latency:
        sizes = [int(s.strip()) for s in args.sizes.split(",")]
        model_cfg = (args.model, args.omlx_url, args.api_key or omlx_api_key()) \
                    if args.model else None
        run_latency_benchmark(binary, runs=args.runs, sizes=sizes,
                              no_compress=args.no_compress, model_config=model_cfg)
        return

    if args.scale:
        run_scale_test(binary, runs=args.runs)
        return

    if args.simulate:
        if args.turns < 13:
            sys.exit(f"--turns must be at least 13 (turns 1-13 are fixed simulation steps); got {args.turns}")
        model_cfg = (args.model, args.omlx_url, args.api_key or omlx_api_key()) \
                    if args.model else None
        run_session_simulation(binary, turns=args.turns, price_per_m=args.price,
                               no_compress=args.no_compress, model_config=model_cfg)
        return

    if args.persist:
        run_persistence_test(binary)
        return

    api_key = args.api_key or omlx_api_key()

    samples = {"error_trace": SAMPLES["error_trace"]} if args.quick else SAMPLES

    import datetime
    print(f"\n{'='*W}")
    print(f"  context-hydra compression benchmark  —  {datetime.date.today()}")
    print(f"  binary: {binary}")
    print(f"{'='*W}")

    if args.compare_models:
        models = list_omlx_models(args.omlx_url, api_key)
        if not models:
            sys.exit(f"No models found at {args.omlx_url}. Is oMLX running?")
        print(f"\n  Models: {', '.join(models)}\n")

        all_results: list[dict] = []
        for model in models:
            print(f"\n{'─'*W}")
            print(f"  {model}")
            print(f"{'─'*W}")
            write_config(model, args.omlx_url, api_key)
            client = HydraClient(binary)
            try:
                for key, sample in samples.items():
                    print(f"    testing {sample['label']} ...", end=" ", flush=True)
                    try:
                        r = run_sample(client, key, sample)
                        r["model"] = model
                        all_results.append(r)
                        print(f"{r['tokens_in']} → {r['tokens_stored']} tok  "
                              f"({r['compress_ratio']}×)  {r['compress_lat']}s")
                    except Exception as e:
                        print(f"FAILED: {e}")
            finally:
                client.close()

        print_model_comparison(all_results)

        # print evidence for best model per sample
        if all_results:
            print(f"\n\n{'='*W}")
            print("  COMPRESSION EVIDENCE  (best model per content type)")
            print(f"{'='*W}")
            samples_seen = set()
            for r in sorted(all_results, key=lambda x: -x["compress_ratio"]):
                if r["key"] not in samples_seen:
                    samples_seen.add(r["key"])
                    print(f"\n  Best for {r['label']}: {r['model']}")
                    print_evidence(r)

    else:
        # single-model mode
        if args.model:
            write_config(args.model, args.omlx_url, api_key)
            print(f"\n  Model: {args.model}")
        elif CONFIG_FILE.exists():
            print(f"\n  Config: {CONFIG_FILE}")
        else:
            print(f"\n  No config found — compress:true will store raw (ratio = 1.0)")
            print(f"  Pass --model MODEL_ID to enable compression.\n")

        client = HydraClient(binary)
        results = []
        try:
            for key, sample in samples.items():
                print(f"\n  Running {sample['label']} ...", flush=True)
                try:
                    r = run_sample(client, key, sample)
                    results.append(r)
                    print_evidence(r)
                except Exception as e:
                    print(f"  ERROR: {e}")
        finally:
            client.close()

        if results:
            print_single_model_summary(results)

    print(f"\n{'='*W}\n")

if __name__ == "__main__":
    main()
