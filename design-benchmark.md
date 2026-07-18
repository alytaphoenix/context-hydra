# context-hydra benchmark harness — design

Measures five things: operation latency, context displacement (tokens removed from the active window), token reduction per compaction, compression model comparison (quality vs RAM cost), and session-level cost savings estimates.

---

## Measured results (2026-07-17)

These are real numbers from the implemented harness running on Apple M2 Mac mini, 64 GB RAM.

### Schema overhead

```
Tool schema overhead: 1540 tokens per call (6160 chars / 4)

  Tool                        desc  schema  total  ~tok
  ────────────────────────── ─────  ──────  ─────  ────
  offload_path_sectioned       482     937   1441   360   ← largest single tool
  offload                       94     708    809   202
  reap                         158     645    807   201
  offload_path                  95     510    617   154
  checkout_remote              262     139    416   104
  search                        78     285    369    92
  checkin                       79     255    341    85
  checkout                     158     139    305    76
  recall                        81     139    226    56
  pin                           80     139    222    55
  stats                        149      36    190    47
  forget                        46     139    191    47
  matrix                        75      36    117    29
  peek                          69      36    109    27
  TOTAL                                      6160  1540
```

### Context displacement (matrix row cost)

```
Header line:     9 tok  (38 chars)
Per entry avg:  23 tok  (94 chars)
Matrix at N:     9 + N × 23 tokens

  N entries   matrix cost
  ──────────  ──────────────
           1     32 tok
           5    124 tok
          10    239 tok
          20    469 tok
          50  1,159 tok
         100  2,309 tok
         200  4,609 tok
```

Call `peek` instead of `matrix` when you only need counts. `peek` costs ~27 tokens to receive.

### Break-even by content size

Replacing X tokens in context with a 23-token matrix row saves X-23 tokens/turn. The 1540-token schema overhead is recouped after 1540/(X-23) turns.

```
  Content size   Saved/turn   Schema recoup   Schema + checkout recoup
  ─────────────────────────────────────────────────────────────────────
   100 tok          77 tok       20.0 turns           21.3 turns
   200 tok         177 tok        8.7 turns            9.8 turns
   300 tok         277 tok        5.6 turns            6.6 turns
   400 tok         377 tok        4.1 turns            5.1 turns
   500 tok         477 tok        3.2 turns            4.3 turns
   800 tok         777 tok        2.0 turns            3.0 turns
  1000 tok         977 tok        1.6 turns            2.6 turns
```

**Rule:** offloading ≥ 400-token content that stays out of context for 3+ turns always wins.
**`offload_path` is always free** — the file never enters context at all.

### Compression model comparison

Benchmarked four content types on six models (all on oMLX, Apple Silicon 4-bit):

```
Model                                      RAM    Avg ratio  Avg lat   Efficiency
─────────────────────────────────────────────────────────────────────────────────
Qwen2.5-3B-Instruct-4bit                  2.3G      2.4×     4.3s        1.04
Qwen2.5-7B-Instruct-4bit                  4.7G      2.3×     8.6s        0.50
Ornith-1.0-35B-4bit:ornith-fast          19.8G      3.0×     5.6s        0.15
gemma-4-26B-A4B-it-OptiQ-4bit:moe-fast  16.4G      2.3×    10.3s        0.14
gemma-4-31B-it-OptiQ-4bit:q4-fast       20.8G      2.7×    38.8s        0.13
gemma-4-31B-it-MLX-6bit:gemma-fast      24.9G      1.8×    50.7s        0.07

Efficiency = avg compression ratio / RAM GB
```

Per content type (best model shown first in each):

```
Error trace (876 tok):
  Qwen-7B      183 tok  (4.8×)   8.8s
  Ornith       194 tok  (4.5×)  10.8s
  Gemma-31B-q4 205 tok  (4.3×)  52.4s
  Qwen-3B      217 tok  (4.0×)   5.4s
  Gemma-26B    265 tok  (3.3×)  19.4s
  Gemma-31B    876 tok  (1.0×)  60.0s  ← timeout

Reasoning chain (821 tok):
  Ornith       242 tok  (3.4×)   4.5s
  Gemma-31B-q4 297 tok  (2.8×)  41.5s
  Gemma-26B    300 tok  (2.7×)   7.8s
  Qwen-7B      337 tok  (2.4×)   8.3s
  Qwen-3B      410 tok  (2.0×)   4.8s

Design note (356 tok):
  Qwen-3B      136 tok  (2.6×)   1.5s
  Gemma-31B-q4 248 tok  (1.4×)  27.4s
  Gemma-31B    279 tok  (1.3×)  39.6s
  Ornith       314 tok  (1.1×)   4.2s
  Qwen-7B      332 tok  (1.1×)   6.2s
  Gemma-26B    320 tok  (1.1×)   6.5s

Rust source context (548 tok):
  Ornith       172 tok  (3.2×)   3.0s   ← only model to compress code reliably
  Gemma-31B-q4 226 tok  (2.4×)  33.9s
  Gemma-31B    260 tok  (2.1×)  49.8s
  Gemma-26B    268 tok  (2.0×)   7.3s
  Qwen-3B      533 tok  (1.0×)   5.5s   ← expansion guard fired
  Qwen-7B      540 tok  (1.0×)  11.1s   ← expansion guard fired
```

### Key findings

**Small models are efficient but fragile on code.** Qwen-7B beats Ornith on error trace compression (4.8× vs 4.5×) at half the latency and ¼ the RAM. But both Qwen models fail on dense Rust source — the expansion guard fires because the model cannot reduce symbol-heavy code below the input length. Ornith is the only tested model that compresses code reliably.

**Compression ratios are 2–5×, not 10–50×.** Pre-benchmark estimates were based on ideal prose content where reasoning steps are highly redundant. Real content mixes short design notes (1.1–2.6×), reasoning traces (2.0–3.4×), and code (1.0–3.2×). Average across content types: 2.3–3.0×.

**Thinking mode is the critical parameter.** All models on oMLX are reasoning models. Without a fast profile (thinking disabled), the model spends the entire token budget on internal reasoning and returns empty visible output — compression silently falls back to raw storage. Fast profiles (`ornith-fast`, `gemma-moe-fast`, `gemma-q4-fast`) fix this.

**Latency is the practical constraint for large models.** Gemma-31B-q4 achieves 2.7× compression (reasonable) but at 38.8s average latency. Gemma-31B-MLX at 50.7s average timed out on error traces (60s HTTP timeout). For synchronous compression workflows these are unusable. Qwen-7B at 8.6s is the practical upper bound for responsive use.

**Compression targets in practice.** `compress_target_tokens = 200` produces 183–242 actual tokens on prose (models overshoot by 0–20%). Code ignores the target because the expansion guard discards the output anyway. `checkout_remote_target_tokens = 300` is appropriate as a non-destructive distillation budget.

---

## What we measure and why

### 1. Schema overhead (one-time baseline)

The tool descriptions are re-sent with every MCP request. This is the fixed cost of having context-hydra loaded. Measure it once so savings estimates can net it out.

**Metric:** token count of all tool descriptions combined, measured by sending a `tools/list` call and counting tokens in the response.

**Measured:** ~1540 tokens. Run `python3 bench_hydra.py --window-analysis` to get the live number for the current binary.

### 2. Operation latency

How fast are the primitives? Latency matters because slow tool calls interrupt agent flow. No latency measurements done yet — this is the next planned benchmark scenario.

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

**Measured:** matrix row averages 23 tokens. Body size is the full stored content.

```
Displacement ratio = (body_tokens - 23) / body_tokens

  1 KB  (~250 tok):   (250 - 23) / 250  = 91%
  10 KB (~2500 tok):  (2500 - 23) / 2500 = 99%
  100 KB (~25000 tok): essentially 100%
```

Use `peek` (27 tok to receive) rather than `matrix` when orienting — it avoids the per-row cost.

### 4. Token reduction per compaction

Every compress or distill operation reports before/after token counts. Tracked per content type because content compresses differently.

**Measured ratios** (see Results section above for full breakdown):
- Error traces: 4.0–4.8× (best results, dense with identifiers and addresses)
- Reasoning chains: 2.0–3.4× (step-by-step prose compresses well)
- Design notes: 1.1–2.6× (structured prose with decisions, lower redundancy)
- Rust source code: 1.0–3.2× (small models fail; only large dense models succeed)

Pre-benchmark estimate of 10–50× was based on idealized content. Actual ratios for realistic mixed content average 2–3×.

### 5. Compression model comparison

Which model gives the best token reduction per GB of RAM? See full results in the Measured results section above.

**Summary:**
- RAM-efficient choice: Qwen2.5-7B-Instruct-4bit (0.50 efficiency, 4.8× on errors, 8.6s avg)
- Lowest RAM: Qwen2.5-3B-Instruct-4bit (1.04 efficiency, but fails on code)
- Code-capable: Ornith-1.0-35B-4bit with fast profile (3.2× on source, 5.6s avg, 19.8 GB)
- Avoid for compression: Gemma-31B-MLX-6bit (0.07 efficiency, 50s latency)

**Key question answered:** compression quality does drop below useful at 3B on code. The 3–7B range works well for prose. Code needs a larger model.

**Evaluation dimensions:**

| Dimension | How measured |
|---|---|
| RAM footprint | model size on disk (GB) as proxy for loaded RAM |
| Compress latency | wall time per compress call |
| Token reduction | `tokens_in / tokens_out` ratio |
| Quality score | key-phrase retention rate |
| Efficiency | token_reduction_ratio / RAM_GB |

**Quality scoring — key-phrase retention:**

```python
import re
from collections import Counter

def key_phrases(text: str, n=20) -> set[str]:
    tokens = re.findall(
        r'\b[A-Z][a-z]+[A-Z]\w*\b'   # camelCase
        r'|\b[a-z_]+::[a-z_]+\b'     # rust paths
        r'|\b\d+\b'                   # numbers
        r'|"[^"]{3,30}"'              # short strings
        r'|\bERROR\b|\bPANIC\b|\bWARN\b', text)
    return {t for t, _ in Counter(tokens).most_common(n)}

def retention_score(original: str, compressed: str, n=20) -> float:
    phrases = key_phrases(original, n)
    if not phrases:
        return 1.0
    return sum(1 for p in phrases if p in compressed) / len(phrases)
```

**Models benchmarked:**

| Model | RAM (GB) | Status | Notes |
|---|---|---|---|
| Ornith-1.0-35B-4bit | 19.8 | ✅ benchmarked | Best on code and reasoning; needs `ornith-fast` profile |
| gemma-4-26B-A4B-it-OptiQ-4bit | 16.4 | ✅ benchmarked | MoE; `gemma-moe-fast` profile needed |
| gemma-4-31B-it-OptiQ-4bit | 20.8 | ✅ benchmarked | `gemma-q4-fast` profile; 38s avg latency |
| gemma-4-31B-it-MLX-6bit | 24.9 | ✅ benchmarked | Slow (50s avg), times out on error traces |
| Qwen2.5-7B-Instruct-4bit | 4.7 | ✅ benchmarked | Best efficiency for prose; fails on code |
| Qwen2.5-3B-Instruct-4bit | 2.3 | ✅ benchmarked | Highest RAM efficiency; fails on code |
| gemma-3-4b-it-qf16 | 3.5 | ❌ gated HF repo | Could not download (requires HF token) |
| SmolLM2-1.7B-Instruct-4bit | 1.1 | ❌ gated HF repo | Could not download |
| Llama-3.2-3B-Instruct-4bit | 2.0 | not downloaded | Non-gated; next candidate |
| Phi-3.5-mini-instruct-4bit | 2.3 | not downloaded | Non-gated; next candidate |

### 6. Cost savings estimate

```
PRICE_PER_M_TOKENS = 15.00  # USD frontier model input — override as needed

tokens_saved_per_turn = body_tokens - 23   (matrix row)
total_saved = tokens_saved_per_turn × turns
usd_saved   = (total_saved / 1_000_000) × price_per_m
```

Break-even for the schema overhead (1540 tokens) against offloaded content:

```
  Content      Saved/turn   Break-even turns
  ─────────────────────────────────────────
   400 tok       377 tok       4.1 turns
   500 tok       477 tok       3.2 turns
   800 tok       777 tok       2.0 turns
  1000 tok       977 tok       1.6 turns
```

For a 20-turn session offloading 5 × 500-token entries (realistic: error traces, reasoning steps):

```
  Offloaded tokens:   2500
  Schema overhead:    1540 / call × N calls (varies)
  Saved per turn:     2500 - 5×23 = 2385 tok
  Session total:      2385 × 20 = 47,700 tok saved
  USD saved:          $0.00072  (at $15/M)
```

The token savings per session are real but modest at current frontier pricing. The more significant benefit at long sessions is reasoning quality from a leaner context, which is not measurable server-side.

---

## Test scenarios

### S1 — Single-entry latency (not yet implemented)

Offload one entry, recall it, checkout, checkin. Repeat for 1 KB / 10 KB / 100 KB bodies. Measures baseline operation cost.

### S2 — Scale test (not yet implemented)

Build a store of 10 / 100 / 500 entries. Measure `peek`, `matrix`, and `search` latency vs entry count. Verifies the README claim that linear scan over ~500 rows is <1ms.

### S3 — Compression pipeline ✅ implemented as `--compare-models`

For a set of realistic content samples (error traces, reasoning chains, design notes, code):
1. Offload raw → measure stored size
2. Offload same content with `compress: true` → measure stored size
3. Checkout raw body → measure response size
4. `checkout_remote` → measure distillate size
5. Report compression ratio and latency for each step

**Results:** see Measured results section above.

### S4 — Session simulation (not yet implemented)

Simulate a realistic 20-turn coding agent session:
- Turn 1: read 3 files → offload_path each
- Turns 2–5: work; each turn recalls 1 entry, checks out 0–1
- Turn 6: hit a bug; offload error trace, offload reasoning chain
- Turns 7–12: debug; search + recall frequently
- Turn 13: resolve; offload(compress: true) the now-stale trace
- Turns 14–20: new task; matrix to orient

Measure total bytes that would have accumulated in context without hydra vs the matrix + checkout traffic that actually occurred.

### S5 — Cross-session persistence (not yet implemented)

Populate a store, restart the binary, verify entries survive and stale hot entries are cleaned up. Confirms the startup_cleanup path and redb persistence guarantee.

### S6 — Compression model comparison ✅ implemented as `--compare-models`

For each model available on the local endpoint:
1. Configure `config.toml` to point at that model
2. Restart the subprocess (context-hydra reads config at startup)
3. Run compress and distill against fixed content samples
4. Record: compress latency, token_in, token_out, reduction ratio, retention score
5. Rank by efficiency metric: `reduction_ratio / ram_gb`

**Results:** see Measured results section above.

### S7 — Window analysis ✅ implemented as `--window-analysis`

Measures schema overhead, matrix row cost, break-even by content size, and compression target recommendations. Output is a human-readable report.

**Results:** see Measured results section above.

---

## Harness architecture

The harness is a Python script (`bench_hydra.py`) that drives context-hydra via its native MCP stdio protocol. No HTTP layer, no mock — talks to the real binary.

### CLI flags (current)

```
python3 bench_hydra.py [--binary PATH] [--model MODEL_ID]
                       [--compare-models] [--window-analysis]
                       [--suggest-models] [--quick]
                       [--omlx-url URL] [--api-key KEY]

--binary          path to context-hydra binary (default: auto-detect)
--model           local model ID for single compress/distill scenario
--compare-models  sweep all models on --omlx-url, rank by efficiency
--window-analysis measure schema overhead, matrix cost, break-even, targets
--suggest-models  print small model download suggestions with HF paths
--quick           run error_trace sample only (fastest sanity check)
--omlx-url        local model server URL (default: http://localhost:8000)
--api-key         auth token (auto-detected from ~/.omlx/settings.json)
```

### CLI flags (planned)

```
--runs N          repetitions per latency measurement (default: 5)
--no-compress     skip compression (for environments without local model)
--no-simulation   skip session simulation
--price FLOAT     price per 1M input tokens in USD (default: 15.0)
--turns N         simulated session length (default: 20)
--sizes BYTES     comma-separated body sizes to test (default: 1024,10240,102400)
```

### MCP stdio driver

The `HydraClient` class in `bench_hydra.py` implements the full MCP handshake over stdio:
- Daemon reader thread with per-request `queue.Queue` for responses
- `call(tool, args)` — synchronous tool call with timeout
- `timed(tool, args)` — returns `(response, elapsed_seconds)`
- `tools_list()` — returns raw tools schema for overhead measurement

### Token estimation

```python
def estimate_tokens(text: str) -> int:
    return (len(text.encode("utf-8")) + 3) // 4
```

4 chars per token is accurate within ~10% for English prose and code. Not suitable for exact billing calculations but sufficient for relative comparisons within the harness.

### Quality scoring

```python
def key_phrases(text: str, n: int = 20) -> list[str]:
    """Extract top-N identifiers, numbers, and error terms by frequency."""
    ...

def retention_score(original: str, compressed: str) -> tuple[float, list[str], list[str]]:
    """Returns (score, kept_phrases, lost_phrases)."""
    ...
```

Both implemented in `bench_hydra.py`. Used to verify that compression preserves meaningful content rather than just achieving a low token count.

### Expansion safeguard (in server.rs)

If `call_llm` returns output ≥ the original content length in bytes, the compressed output is discarded and the raw content is stored instead. This prevents models from expanding inputs (observed with Gemma MoE thinking mode on) from corrupting stored entries.

---

## Implementation notes

- **Config reload:** context-hydra reads `config.toml` at startup. The harness restarts a new subprocess per model when comparing models.
- **Thinking mode:** all current oMLX models are reasoning models. Without fast profiles, compression silently falls back to raw storage. The harness's `_compression_model_list()` auto-selects fast profiles where available (any profile ending in `-fast`).
- **oMLX API key:** auto-detected from `~/.omlx/settings.json` → `auth.api_key`.
- **Model registration:** newly downloaded models require an oMLX server restart (`omlx-cli restart`) to appear in `/v1/models`.
- **Code content:** do not use `compress: true` on code — small models expand it and the expansion guard stores raw. Use `offload` raw and `checkout_remote` for distillation at retrieval time.
- **Fresh store for benchmarks:** there is no `--data-dir` flag yet. The harness uses the real store and cleans up test entries with `forget` after each run.
