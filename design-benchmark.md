# context-hydra benchmark harness — design

Measures five things: operation latency, context displacement (tokens removed from the active window), token reduction per compaction, compression model comparison (quality vs RAM cost), and session-level cost savings estimates.

---

## What we measure and why

### 1. Schema overhead (one-time baseline)

The tool descriptions are re-sent with every MCP request. This is the fixed cost of having context-hydra loaded. Measure it once so the savings estimates can net it out.

**Metric:** token count of all tool descriptions combined, measured by sending a `tools/list` call and counting tokens in the response.

### 2. Operation latency

How fast are the primitives? Latency matters because slow tool calls interrupt agent flow.

| Operation | What to measure |
|---|---|
| `offload` (no compress) | wall time from call → response |
| `offload` (compress: true) | same, isolates local model RTT |
| `offload_path` | same |
| `checkout` | disk read + DB update |
| `checkout_remote` | same, isolates distillation RTT |
| `checkin` | DB update only |
| `peek` | DB scan, count only |
| `matrix` | DB scan, full index |
| `search` | DB scan with filter |
| `recall` | DB read, no body pull |

Each operation runs against three content sizes: small (1 KB), medium (10 KB), large (100 KB). Latency is median of 5 runs, with p95 reported.

### 3. Context displacement

The core value claim: content offloaded to hydra leaves the active window. The matrix row is tiny compared to the full body.

**Displacement ratio:**
```
displaced_tokens = (body_bytes - matrix_row_bytes) / 4
displacement_pct = displaced_tokens / (body_bytes / 4) × 100
```

Matrix row size is measured empirically from the `matrix` output (~15–20 tokens per row). Body size is the actual stored content.

### 4. Token reduction per compaction

Every compress or distill operation should report before/after token counts so the reduction is visible per call, not just in aggregate. This is the number that goes in the agent's feedback loop.

```
tokens_in   = estimate_tokens(original_content)
tokens_out  = estimate_tokens(stored_or_distilled_content)
reduction   = tokens_in - tokens_out
reduction_pct = reduction / tokens_in × 100
```

Tracked per content type (error trace, reasoning chain, design note, code context) because different content compresses differently. Reasoning chains tend to compress 10–50×; structured code context much less.

### 5. Compression model comparison

Which model gives the best token reduction per GB of RAM? This is the key question for choosing the local compression model. A 3B MoE model that achieves 80% of the compression quality of a 35B dense at 15% of the RAM cost is a clear winner.

**Evaluation dimensions:**

| Dimension | How measured |
|---|---|
| RAM footprint | model size on disk (GB) as proxy for loaded RAM |
| Compress latency | wall time for a single compress call at 10 KB input |
| Distill latency | wall time for a single distill call at 10 KB input |
| Token reduction | `tokens_in / tokens_out` ratio |
| Quality score | key-phrase retention rate (see below) |

**Quality scoring — key-phrase retention:**

After compressing a known piece of content, extract the top-N important phrases from the original (nouns, identifiers, numbers, error messages) and check what fraction appear verbatim in the compressed output. Not perfect, but objective and fast.

```python
import re

def key_phrases(text: str, n=20) -> set[str]:
    # Identifiers, numbers, quoted strings, error terms
    tokens = re.findall(r'\b[A-Z][a-z]+[A-Z]\w*\b'   # camelCase
                       r'|\b[a-z_]+::[a-z_]+\b'       # rust paths
                       r'|\b\d+\b'                     # numbers
                       r'|"[^"]{3,30}"'                # short strings
                       r'|\bERROR\b|\bPANIC\b|\bWARN\b', text)
    # Return top-N by frequency
    from collections import Counter
    return {t for t, _ in Counter(tokens).most_common(n)}

def retention_score(original: str, compressed: str, n=20) -> float:
    phrases = key_phrases(original, n)
    if not phrases:
        return 1.0
    retained = sum(1 for p in phrases if p in compressed)
    return retained / len(phrases)
```

**Models to compare** (those available on the local oMLX server):

| Model | RAM (GB) | Arch | Notes |
|---|---|---|---|
| Ornith-1.0-35B-4bit | 19.8 | dense | current default |
| gemma-4-26B-A4B-it-OptiQ-4bit | 16.4 | MoE | 4B active, fast |
| gemma-4-31B-it-OptiQ-4bit | 20.8 | dense | OptiQ mixed quant |
| gemma-4-31B-it-MLX-6bit | 24.9 | dense | higher quality baseline |

If Qwen3-Coder-30B-A3B is downloaded (~17 GB), include it — it's purpose-built for structured content which may compress more faithfully.

**Expected output shape:**

```
COMPRESSION MODEL COMPARISON
Content: error trace (10 KB, ~2500 tokens)

Model                           RAM     Compress  Distill   Tok reduction  Retention
                                (GB)    latency   latency   (ratio / %)    score
──────────────────────────────────────────────────────────────────────────────────────
Ornith-1.0-35B-4bit             19.8    1.1s      0.8s      8.3× / 88%     0.91
gemma-4-26B-A4B-it-OptiQ-4bit   16.4    0.4s      0.3s      7.1× / 86%     0.87
gemma-4-31B-it-OptiQ-4bit       20.8    1.4s      1.0s      8.8× / 89%     0.93
gemma-4-31B-it-MLX-6bit         24.9    4.1s      2.9s      9.1× / 89%     0.94

Efficiency (token_reduction_ratio / RAM_GB):
  gemma-4-26B-A4B-it-OptiQ-4bit   0.43  ← best tokens-saved per GB
  gemma-4-31B-it-OptiQ-4bit       0.42
  Ornith-1.0-35B-4bit             0.42
  gemma-4-31B-it-MLX-6bit         0.37
```

The efficiency metric `token_reduction_ratio / RAM_GB` is the primary ranking signal — it answers "how much compression per GB of RAM invested?"

### 7. Cost savings estimate

Assumptions (parameterizable):
- Token ≈ 4 bytes (GPT-4 / Claude approximation)
- Session = N turns, each turn re-sends the full context window
- Pricing: configurable per model (default: $15/M input tokens for a frontier model)

```
tokens_in_body  = body_bytes / 4
tokens_in_row   = ~18  (matrix row, empirically measured)
displaced_per_turn = tokens_in_body - tokens_in_row
cost_saved_per_session = displaced_per_turn × turns × price_per_token
```

For compress path:
```
compressed_tokens  = stored_bytes / 4
displaced_per_turn = tokens_in_body - compressed_tokens
```

---

## Test scenarios

### S1 — Single-entry latency

Offload one entry, recall it, checkout, checkin. Repeat for 1 KB / 10 KB / 100 KB bodies. Measures baseline operation cost.

### S2 — Scale test

Build a store of 10 / 100 / 500 entries. Measure `peek`, `matrix`, and `search` latency vs entry count. Verifies the README claim that linear scan over ~500 rows is <1ms.

### S3 — Compression pipeline

For a set of realistic content samples (error traces, reasoning chains, design notes):
1. Offload raw → measure stored size
2. Offload same content with `compress: true` → measure stored size
3. Checkout raw body → measure response size
4. `checkout_remote` → measure distillate size
5. Report compression ratio and latency for each step

### S4 — Session simulation

Simulate a realistic 20-turn coding agent session:
- Turn 1: read 3 files → offload_path each
- Turns 2–5: work; each turn recalls 1 entry, checks out 0–1
- Turn 6: hit a bug; offload error trace, offload reasoning chain
- Turns 7–12: debug; search + recall frequently
- Turn 13: resolve; offload(compress: true) the now-stale trace
- Turns 14–20: new task; matrix to orient

Measure total bytes that would have accumulated in context without hydra vs the matrix + checkout traffic that actually occurred. Compute tokens and cost delta.

### S5 — Cross-session persistence

Populate a store, restart the binary, verify entries survive and stale hot entries are cleaned up. Confirms the startup_cleanup path and redb persistence guarantee.

### S6 — Compression model comparison

For each model available on the oMLX server:
1. Configure `config.toml` to point at that model
2. Reload context-hydra (restart the subprocess — it reads config at startup)
3. Run compress and distill against the same 5 fixed content samples:
   - `trace_10k`: a 10 KB error trace with stack frames, identifiers, line numbers
   - `reason_20k`: a 20 KB reasoning chain (thinking block style)
   - `design_5k`: a 5 KB design note with decisions and tradeoffs
   - `code_15k`: a 15 KB code context (function + surrounding file)
   - `mixed_30k`: mixed content, 30 KB

4. Record: compress latency, distill latency, token_in, token_out, reduction ratio, retention score
5. Rank by efficiency metric: `reduction_ratio / ram_gb`

Content samples are deterministic (seeded, pre-generated) so results are comparable across runs and machines.

---

## Harness architecture

The harness is a Python script (`bench_hydra.py`) that drives context-hydra via its native MCP stdio protocol. No HTTP layer, no mock — talks to the real binary.

### MCP stdio driver

```python
import subprocess, json, threading, queue, time

class HydraClient:
    def __init__(self, binary: str):
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0
        self._pending = {}
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._init()

    def _next_id(self):
        self._id += 1
        return self._id

    def _send(self, msg: dict):
        line = json.dumps(msg) + "\n"
        self.proc.stdin.write(line.encode())
        self.proc.stdin.flush()

    def _read_loop(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            if "id" in msg and msg["id"] in self._pending:
                self._pending[msg["id"]].put(msg)

    def _rpc(self, method: str, params: dict, timeout=30) -> dict:
        id_ = self._next_id()
        q = queue.Queue()
        self._pending[id_] = q
        self._send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})
        result = q.get(timeout=timeout)
        del self._pending[id_]
        return result

    def _init(self):
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "bench", "version": "0.1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool: str, args: dict, timeout=60) -> str:
        resp = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["result"]["content"][0]["text"]

    def timed(self, tool: str, args: dict, timeout=60) -> tuple[str, float]:
        t0 = time.perf_counter()
        result = self.call(tool, args, timeout=timeout)
        return result, time.perf_counter() - t0

    def close(self):
        self.proc.stdin.close()
        self.proc.wait()
```

### Synthetic content generator

```python
import random, string

def gen_content(size_bytes: int, style="trace") -> str:
    """Generate realistic-looking content of a target byte size."""
    if style == "trace":
        # Simulate an error trace
        lines = [
            "ERROR: connection timeout after 30s",
            "  at src/client.rs:142 in fn send_request",
            "  at src/server.rs:87 in fn handle_connection",
            "caused by: io::Error: connection refused (os error 111)",
        ]
        base = "\n".join(lines) + "\n"
    elif style == "reasoning":
        # Simulate a reasoning chain
        base = ("Let me think through this step by step. "
                "First, I need to consider the initial conditions. "
                "The constraint is that X must be satisfied before Y. "
                "Working backwards from the goal... ") * 4
    else:
        base = "Design note: " + " ".join(
            random.choices(string.ascii_letters, k=40)
        ) + "\n"
    
    # Repeat to hit target size
    repeats = max(1, size_bytes // len(base)) + 1
    return (base * repeats)[:size_bytes]
```

### Measurement loop

```python
import statistics

def measure_op(client, tool, args, runs=5, timeout=60):
    latencies = []
    for _ in range(runs):
        _, t = client.timed(tool, args, timeout=timeout)
        latencies.append(t * 1000)  # ms
    return {
        "median_ms": statistics.median(latencies),
        "p95_ms":    sorted(latencies)[int(len(latencies) * 0.95)],
        "min_ms":    min(latencies),
    }
```

### Token estimation

```python
def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4

def matrix_row_tokens(matrix_output: str, n_entries: int) -> float:
    """Avg tokens per matrix row from live output."""
    total = estimate_tokens(matrix_output)
    header_lines = 1
    return total / max(n_entries, 1)
```

### Compression evidence display

Numbers alone don't prove the compression is meaningful — a model that outputs "..." achieves a perfect ratio but zero value. The harness prints a side-by-side excerpt so a human can judge whether the compressed output preserved what matters.

For each content sample in S3 and S6, the output shows:

```
── error trace (10 KB → 200 tok) ────────────────────────────────────────────

  ORIGINAL (first 400 chars):
    ERROR: connection timeout after 30s
      at src/client.rs:142 in fn send_request
      at src/server.rs:87 in fn handle_connection
    caused by: io::Error: connection refused (os error 111)
    [... 9,600 more bytes ...]

  COMPRESSED (full output, 200 tok target):
    Connection timeout (30s) at client.rs:142/send_request →
    server.rs:87/handle_connection. Cause: io::Error ECONNREFUSED (111).

  KEY PHRASES retained (17/20):  connection timeout, 30s, client.rs:142,
    send_request, server.rs:87, handle_connection, io::Error, ECONNREFUSED,
    111  ✓  |  lost: [fn, os error]
  Reduction: 2500 → 200 tok  (12.5×, 92% retained)
```

For the model comparison (S6), the same input produces a column for each model so the outputs can be compared directly:

```
── reasoning chain (20 KB) — model comparison ────────────────────────────────

  ORIGINAL (first 200 chars):
    Let me work through this step by step. The constraint is that auth must
    complete before the database connection is opened. First, check if the
    token is in the cache [...]

  Ornith-1.0-35B-4bit (1.1s):
    Auth must precede DB open. Token cache checked first; on miss, call
    /auth endpoint with client_id. Cache TTL 300s. On failure, abort with
    AuthError and log to stderr. DB uses connection pool (max 10).

  gemma-4-26B-A4B MoE (0.4s):
    Auth precedes DB. Token cache (300s TTL); on miss → /auth(client_id).
    Failure: AuthError + stderr. DB pool max 10.

  gemma-4-31B-it-OptiQ (1.4s):
    Auth required before DB connection. Check token cache (TTL=300s); if
    miss, call /auth with client_id. On failure: AuthError logged to stderr.
    DB connection pool limited to 10.
```

This makes it immediately visible whether a faster/smaller model produces meaningfully worse summaries on your actual content types.

### Token reduction tracker

Every compress/distill call records before and after token counts. The harness accumulates these into a per-content-type summary.

```python
def measure_compression(client, content: str, tool="offload", extra_args=None) -> dict:
    args = {
        "content": content,
        "ctx_type": "trace",
        "topic": "bench",
        "summary": "bench",
        "compress": True,
    }
    if extra_args:
        args.update(extra_args)

    tokens_in = estimate_tokens(content)

    if tool == "offload":
        result, latency = client.timed("offload", args)
        # Retrieve the stored body to measure tokens_out
        entry_id = result.split("\n")[1].split(" | ")[0].strip()
        body, _ = client.timed("checkout", {"id": entry_id})
        # Body is fenced; strip the fence tags
        body_text = re.sub(r"<hydra:body:[0-9a-f]+[^>]*>|</hydra:body:[0-9a-f]+>", "", body).strip()
        tokens_out = estimate_tokens(body_text)
        client.call("checkin", {"id": entry_id, "nonce": re.search(r"checkout nonce: (\w+)", body).group(1)})
    else:  # checkout_remote
        result, latency = client.timed("checkout_remote", {"id": extra_args["id"]})
        tokens_out = estimate_tokens(result)

    return {
        "tokens_in":      tokens_in,
        "tokens_out":     tokens_out,
        "reduction":      tokens_in - tokens_out,
        "reduction_pct":  round((tokens_in - tokens_out) / tokens_in * 100, 1),
        "ratio":          round(tokens_in / max(tokens_out, 1), 1),
        "latency_ms":     round(latency * 1000, 0),
        "retention":      retention_score(content, result),
    }
```

### Cost model

```python
PRICE_PER_M_TOKENS = 15.00  # USD, frontier model input — override as needed

def cost_saved(
    body_bytes: int,
    stored_bytes: int,   # after compression (= body_bytes if no compress)
    row_tokens: float,   # avg matrix row size
    turns: int,
    price_per_m: float = PRICE_PER_M_TOKENS,
) -> dict:
    body_tokens   = body_bytes / 4
    stored_tokens = stored_bytes / 4
    # Without hydra: body_tokens in context every turn
    # With hydra: row_tokens in matrix every turn, stored_tokens only on checkout
    tokens_saved_per_turn = body_tokens - row_tokens
    total_tokens_saved    = tokens_saved_per_turn * turns
    usd_saved             = (total_tokens_saved / 1_000_000) * price_per_m
    compress_ratio        = body_bytes / stored_bytes if stored_bytes > 0 else 1.0
    return {
        "body_tokens":         int(body_tokens),
        "stored_tokens":       int(stored_tokens),
        "row_tokens":          round(row_tokens, 1),
        "tokens_saved_per_turn": int(tokens_saved_per_turn),
        "total_tokens_saved":  int(total_tokens_saved),
        "usd_saved":           round(usd_saved, 4),
        "compress_ratio":      round(compress_ratio, 2),
    }
```

---

## Output format

The harness prints a human-readable report and saves JSON results to `~/.local/share/context-hydra/bench_results.json` (macOS: `~/Library/Application Support/context-hydra/bench_results.json`).

```
================================================================================
  context-hydra benchmark — 2026-07-16
================================================================================

SCHEMA OVERHEAD
  tool descriptions: 1,842 tokens  (~$0.028/M-token model per request)

OPERATION LATENCY  (median / p95 ms, n=5 runs each)
  Content size       1 KB      10 KB     100 KB
  ─────────────────────────────────────────────
  offload             3 /  5   5 /  8    22 / 31
  offload (compress)  850/ 920  1100/1180 2100/2300
  checkout            2 /  3   3 /  4    14 / 18
  checkout_remote     620/ 680  800/ 850 1500/1620
  checkin             1 /  2   1 /  2     1 /  2
  peek                1 /  1   1 /  1     1 /  1
  matrix (100 entries) 3 /  4  —         —
  search (100 entries) 3 /  4  —         —
  recall              1 /  2   1 /  2     1 /  2

CONTEXT DISPLACEMENT  (per offloaded entry)
  Avg matrix row: ~18 tokens
  Content size     Body tokens  Row tokens  Displaced  Pct
  ─────────────────────────────────────────────────────────
  1 KB               250          18          232      93%
  10 KB             2,500         18         2,482     99%
  100 KB           25,000         18        24,982     99.9%

TOKEN REDUCTION PER COMPACTION  (local model: Ornith-1.0-35B-4bit)
  Content               Tokens in  Tokens out  Reduction  Ratio   Retention  Latency
  ────────────────────────────────────────────────────────────────────────────────────
  error trace (10 KB)     2,500        200       2,300    12.5×     0.91      1.1s
  reasoning (20 KB)       5,000        200       4,800    25.0×     0.83      1.4s
  design note (5 KB)      1,250        200       1,050     6.3×     0.94      0.9s
  code context (15 KB)    3,750        200       3,550    18.8×     0.89      1.2s
  mixed (30 KB)           7,500        200       7,300    37.5×     0.86      1.8s

  checkout_remote distillation (same content, stored body → distillate):
  Content               Tokens in  Tokens out  Reduction  Ratio   Retention  Latency
  ────────────────────────────────────────────────────────────────────────────────────
  error trace (10 KB)     2,500        300       2,200     8.3×     0.93      0.8s
  code context (15 KB)    3,750        300       3,450    12.5×     0.91      1.0s

COST SAVINGS ESTIMATE  ($15/M input tokens, 20-turn session)
  Scenario              Tokens saved/turn  Session total  USD saved
  ──────────────────────────────────────────────────────────────────
  1 file (10 KB)              2,482          49,640        $0.00074
  5 files (10 KB ea)         12,410         248,200        $0.00373
  Long session (500 KB total) 124,982      2,499,640       $0.03749
  + compression (50× ratio)  124,782      2,495,640       $0.03743

SCALE TEST  (latency vs store size)
  Entries   peek ms   matrix ms   search ms
  ─────────────────────────────────────────
  10          1          2           2
  100         1          4           4
  500         2         18          14

SESSION SIMULATION  (20-turn coding session, 3 files + traces)
  Without hydra:  ~62,000 tokens accumulated in context
  With hydra:     ~4,200 tokens in active context (matrix + one checkout)
  Net reduction:  ~93%  |  USD saved: $0.0086

================================================================================
Results → ~/Library/Application Support/context-hydra/bench_results.json
```

---

## CLI flags

```
bench_hydra.py [--binary PATH] [--runs N] [--no-compress] [--no-simulation]
               [--price FLOAT] [--turns N] [--sizes 1024,10240,102400]
               [--model MODEL_ID] [--compare-models] [--omlx-url URL]

--binary          path to context-hydra binary (default: $(which context-hydra))
--runs            repetitions per latency measurement (default: 5)
--no-compress     skip compression scenarios (for environments without a local model)
--no-simulation   skip the full session simulation
--price           price per 1M input tokens in USD (default: 15.0)
--turns           simulated session length (default: 20)
--sizes           comma-separated body sizes in bytes to test
--model           local model id for a single compress/distill scenario
--compare-models  run S6 model comparison across all models found on --omlx-url
--omlx-url        oMLX server base URL for model comparison (default: http://localhost:8000)
```

---

## README integration

The benchmark results section in README.md should show one canonical run with real numbers. Structure:

```markdown
## Performance

Results from a single Mac mini (Apple M2, 64 GB RAM) running context-hydra 0.1.

### Operation latency (median)

| Operation | 1 KB | 10 KB | 100 KB |
|---|---|---|---|
| offload | Xms | Xms | Xms |
| checkout | Xms | Xms | Xms |
| recall | Xms | Xms | Xms |

### Context displacement

At 10 KB per entry, a single offload removes ~2,482 tokens from the active window
while adding ~18 tokens to the matrix. Displacement ratio: 99%.

### Cost savings (illustrative)

A 20-turn session working across 5 open files (10 KB each) saves ~248K tokens
per session — roughly $0.004 at $15/M input tokens. At 100 sessions/month that's
~$0.37/month saved, before accounting for reasoning quality improvements from
a leaner context.

See `design-benchmark.md` for methodology and `bench_hydra.py` to run locally.
```

The numbers in the README should come from an actual run, not from this design doc.

---

## Implementation notes

- Use a fresh redb store for each benchmark run: start the binary with `CONTEXT_HYDRA_DATA_DIR=/tmp/bench-$$` (requires adding env var support to the binary) or wipe `~/.local/share/context-hydra/` before each run. The cleaner approach is an `--data-dir` flag on the binary.
- The schema overhead measurement requires listing tools and counting response bytes before any tool calls. Do this as the first operation after `initialized`.
- For compression benchmarks, use `--no-compress` if no local model is running — the harness should detect and skip gracefully rather than fail.
- The session simulation should use deterministic content (seeded RNG) so results are reproducible across machines.
- Run the scale test last — it leaves the store in a populated state that would skew latency measurements if run first.
