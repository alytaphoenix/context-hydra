use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Instant,
};

use anyhow::Result;
use chrono::Utc;
use rmcp::{
    ErrorData,
    handler::server::wrapper::Parameters,
    model::{
        ListResourceTemplatesResult, ListResourcesResult, PaginatedRequestParams,
        ReadResourceRequestParams, ReadResourceResult, Resource, ResourceContents,
        ResourceTemplate, ServerCapabilities, ServerInfo,
    },
    service::{RequestContext, RoleServer},
    tool, tool_handler, tool_router, ServerHandler,
};
use schemars::JsonSchema;
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::store::{MatrixRow, Store};

// ---------- session stats ----------

#[derive(Default)]
struct SessionStats {
    offloads: u32,
    bytes_offloaded: u64,
    ref_offloads: u32,
    bytes_ref_offloaded: u64,
    dedup_hits: u32,
    recalls: u32,
    checkouts: u32,
    checkins: u32,
    matrix_calls: u32,
    peek_calls: u32,
    searches: u32,
}

// ---------- server ----------

pub struct HydraServer {
    store: Arc<Store>,
    stats: Arc<Mutex<SessionStats>>,
    /// In-memory only: tracks when each id was last checked in this session.
    /// Used for min-cold-time warnings. Not persisted — naturally session-scoped.
    checkin_times: Arc<Mutex<HashMap<String, Instant>>>,
}

impl HydraServer {
    pub fn new() -> Result<Self> {
        let store = Arc::new(Store::new()?);
        let cleaned = store.startup_cleanup()?;
        if cleaned > 0 {
            tracing::info!("startup: cleared {cleaned} stale hot entries");
        }
        Ok(Self {
            store,
            stats: Arc::new(Mutex::new(SessionStats::default())),
            checkin_times: Arc::new(Mutex::new(HashMap::new())),
        })
    }
}

// ---------- helpers ----------

const MIN_COLD_SECS: u64 = 30;

fn content_hash(s: &str) -> String {
    Sha256::digest(s.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn estimate_tokens(text: &str) -> u32 {
    (text.len() as u32 + 3) / 4
}

fn short_nonce() -> String {
    uuid::Uuid::new_v4()
        .to_string()
        .replace('-', "")
        .chars()
        .take(8)
        .collect()
}

fn fence_body(nonce: &str, row: &MatrixRow, body: &str) -> String {
    let tag = format!("hydra:body:{nonce}");
    let safe_body = body.replace(&format!("</{tag}>"), &format!("[/{tag}]"));
    format!(
        "<{tag} id=\"{}\" topic=\"{}\" offloaded=\"{}\">\n{}\n</{tag}>",
        row.id,
        row.topic,
        row.created_at.format("%Y-%m-%dT%H:%M:%SZ"),
        safe_body,
    )
}

fn fmt_row(r: &MatrixRow) -> String {
    let tags = if r.tags.is_empty() {
        String::new()
    } else {
        format!(" [{}]", r.tags.join(","))
    };
    format!(
        "{} | {}{} | {} | sal={:.2} | {} | ~{}tok",
        r.id, r.topic, tags, r.ctx_type, r.salience, r.status, r.token_estimate
    )
}

// ---------- params ----------

#[derive(Debug, Deserialize, JsonSchema)]
pub struct OffloadParams {
    pub content: String,
    pub ctx_type: String,
    pub topic: String,
    pub summary: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_salience")]
    pub salience: f64,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct OffloadPathParams {
    /// Absolute path to file. Server reads it — content never enters the agent context.
    pub path: String,
    pub ctx_type: String,
    pub topic: String,
    pub summary: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_salience")]
    pub salience: f64,
}

fn default_salience() -> f64 {
    0.5
}

fn default_split_at() -> u8 {
    2
}

fn default_summary_len() -> usize {
    200
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct SearchParams {
    #[serde(default)]
    pub query: String,
    pub ctx_type: Option<String>,
    pub status: Option<String>,
    pub min_salience: Option<f64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct IdParam {
    pub id: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct CheckinParams {
    pub id: String,
    /// Nonce returned by checkout. Must match to complete checkin.
    pub nonce: String,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct OffloadPathSectionedParams {
    /// Absolute path to the markdown file. Server reads it — content never enters context.
    pub path: String,
    pub ctx_type: String,
    /// Split at headings of this level and above. 1=H1 only, 2=H1+H2 (default), 3=H1–H3.
    #[serde(default = "default_split_at")]
    pub split_at: u8,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_salience")]
    pub salience: f64,
    /// Pin all created entries immediately (sets salience 1.0, survives reap).
    #[serde(default)]
    pub pin: bool,
    /// Max chars from section body to use as auto-summary. Default 200.
    #[serde(default = "default_summary_len")]
    pub summary_len: usize,
}

#[derive(Debug, Deserialize, JsonSchema)]
pub struct ReapParams {
    /// Delete entries older than this many days (based on created_at).
    pub max_age_days: Option<u32>,
    /// Only reap entries with salience at or below this value.
    pub max_salience: Option<f64>,
    /// Status to target. Defaults to "cold". Pinned entries are always spared.
    pub status: Option<String>,
    /// Preview without deleting. Always use this first.
    #[serde(default)]
    pub dry_run: bool,
}

// ---------- markdown splitter ----------

/// Returns `(heading_line, body)` pairs. `heading_line` is the full `## Foo` line
/// (empty string for any preamble before the first heading). `body` contains all
/// lines between headings, NOT including the heading line itself.
/// Tracks ATX code fences (``` / ~~~) so headings inside fenced blocks are skipped.
fn split_markdown_sections(content: &str, split_at: u8) -> Vec<(String, String)> {
    let mut sections: Vec<(String, String)> = Vec::new();
    let mut current_heading: String = String::new();
    let mut current_body: Vec<&str> = Vec::new();
    let mut in_fence = false;
    let mut has_any_section = false;

    for line in content.lines() {
        let trimmed = line.trim();
        let is_fence = trimmed.starts_with("```") || trimmed.starts_with("~~~");
        if is_fence {
            in_fence = !in_fence;
        }

        // Only split on headings outside of fenced blocks and not on fence lines themselves.
        if !is_fence && !in_fence {
            if let Some(level) = markdown_heading_level(line) {
                if level <= split_at as usize {
                    // Save the section accumulated so far (including any preamble).
                    if has_any_section || !current_body.is_empty() {
                        sections.push((current_heading.clone(), current_body.join("\n")));
                    }
                    current_heading = line.to_string();
                    current_body = Vec::new();
                    has_any_section = true;
                    continue;
                }
            }
        }

        current_body.push(line);
    }

    // Flush the final section.
    if has_any_section || !current_body.is_empty() {
        sections.push((current_heading, current_body.join("\n")));
    }

    sections
}

fn markdown_heading_level(line: &str) -> Option<usize> {
    if !line.starts_with('#') {
        return None;
    }
    let level = line.chars().take_while(|&c| c == '#').count();
    let rest = &line[level..];
    // Valid ATX heading: zero or more spaces follow the # run (empty heading is allowed).
    if rest.is_empty() || rest.starts_with(' ') {
        Some(level)
    } else {
        None
    }
}

// ---------- tools ----------

#[tool_router]
impl HydraServer {
    /// Offload content out of active context. Deduplicates by content hash. Returns a matrix pointer.
    #[tool]
    async fn offload(&self, Parameters(p): Parameters<OffloadParams>) -> Result<String, String> {
        let bytes = p.content.len() as u64;
        let (_, msg) = self
            .do_offload(
                p.content, p.ctx_type, p.topic, p.summary, p.tags, p.salience, false, false, bytes,
            )
            .await?;
        Ok(msg)
    }

    /// Offload a file by path — server reads it, content never enters context. Strongest displacement.
    #[tool]
    async fn offload_path(
        &self,
        Parameters(p): Parameters<OffloadPathParams>,
    ) -> Result<String, String> {
        let path = std::path::PathBuf::from(&p.path);
        let content = tokio::fs::read_to_string(&path)
            .await
            .map_err(|e| format!("cannot read {}: {e}", p.path))?;
        let bytes = content.len() as u64;
        let (_, msg) = self
            .do_offload(
                content, p.ctx_type, p.topic, p.summary, p.tags, p.salience, false, true, bytes,
            )
            .await?;
        Ok(msg)
    }

    /// Offload a markdown file split by headings — each section becomes a separate entry.
    /// Server reads the file; content never enters context. Code fences are tracked so
    /// headings inside fenced blocks are not treated as split points. Sections with fewer
    /// than 20 non-whitespace chars of body are skipped. The filename is auto-added as a
    /// tag for easy retrieval via search. On re-runs, changed sections accumulate new
    /// entries; use search(query: filename) then forget to clear stale ones first.
    #[tool]
    async fn offload_path_sectioned(
        &self,
        Parameters(p): Parameters<OffloadPathSectionedParams>,
    ) -> Result<String, String> {
        let path = std::path::PathBuf::from(&p.path);
        let content = tokio::fs::read_to_string(&path)
            .await
            .map_err(|e| format!("cannot read {}: {e}", p.path))?;

        let filename = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("unknown")
            .to_string();

        let sections = split_markdown_sections(&content, p.split_at);

        let mut results: Vec<String> = Vec::new();
        let mut skipped = 0usize;

        for (heading_line, body) in sections {
            let non_ws = body.chars().filter(|c| !c.is_whitespace()).count();
            if non_ws < 20 {
                skipped += 1;
                continue;
            }

            let topic = if heading_line.is_empty() {
                format!("{filename} (preamble)")
            } else {
                heading_line.trim_start_matches('#').trim().to_string()
            };

            let summary: String = body
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .chars()
                .take(p.summary_len)
                .collect();

            let mut tags = p.tags.clone();
            if !tags.contains(&filename) {
                tags.push(filename.clone());
            }

            let full_content = if heading_line.is_empty() {
                body.clone()
            } else {
                format!("{heading_line}\n{body}")
            };
            let bytes = full_content.len() as u64;

            let (_, msg) = self
                .do_offload(
                    full_content,
                    p.ctx_type.clone(),
                    topic,
                    summary,
                    tags,
                    p.salience,
                    p.pin,
                    true,
                    bytes,
                )
                .await?;
            results.push(msg);
        }

        if results.is_empty() {
            return Ok(format!(
                "no sections with substantial content (skipped {skipped})"
            ));
        }

        Ok(format!(
            "offloaded {} sections from {} ({skipped} skipped)\n{}",
            results.len(),
            filename,
            results.join("\n")
        ))
    }

    /// Search the matrix by keyword, type, status, or salience. Returns compact rows.
    #[tool]
    async fn search(&self, Parameters(p): Parameters<SearchParams>) -> Result<String, String> {
        let store = self.store.clone();
        let rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        self.stats.lock().unwrap().searches += 1;

        let q = p.query.to_lowercase();
        let filtered: Vec<&MatrixRow> = rows
            .iter()
            .filter(|r| {
                let text_match = q.is_empty()
                    || r.topic.to_lowercase().contains(&q)
                    || r.summary.to_lowercase().contains(&q)
                    || r.tags.iter().any(|t| t.to_lowercase().contains(&q));
                let type_match = p.ctx_type.as_deref().map_or(true, |t| r.ctx_type == t);
                let status_match = p.status.as_deref().map_or(true, |s| r.status == s);
                let sal_match = p.min_salience.map_or(true, |m| r.salience >= m);
                text_match && type_match && status_match && sal_match
            })
            .collect();

        if filtered.is_empty() {
            return Ok("no matches".to_string());
        }
        Ok(filtered
            .iter()
            .map(|r| fmt_row(r))
            .collect::<Vec<_>>()
            .join("\n"))
    }

    /// Read an entry's summary without pulling the full body. No nonce, no state change.
    #[tool]
    async fn recall(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let store = self.store.clone();
        let id = p.id.clone();
        let row = tokio::task::spawn_blocking(move || store.get(&id))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?
            .ok_or_else(|| format!("not found: {}", p.id))?;
        self.stats.lock().unwrap().recalls += 1;
        Ok(format!("{}\nsummary: {}", fmt_row(&row), row.summary))
    }

    /// Pull full body into active context. Issues a nonce; marks entry hot. Body is fenced.
    /// Warns if entry was checked in recently — use recall first to avoid churn.
    #[tool]
    async fn checkout(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let warning = {
            let times = self.checkin_times.lock().unwrap();
            if let Some(t) = times.get(&p.id) {
                let elapsed = t.elapsed().as_secs();
                if elapsed < MIN_COLD_SECS {
                    Some(format!(
                        "note: checked in {elapsed}s ago — consider recall first\n"
                    ))
                } else {
                    None
                }
            } else {
                None
            }
        };

        let body_path = self.store.body_path(&p.id);
        if !body_path.exists() {
            return Err(format!("not found: {}", p.id));
        }
        let content = tokio::fs::read_to_string(&body_path)
            .await
            .map_err(|e| e.to_string())?;

        let nonce = short_nonce();
        let nonce_c = nonce.clone();
        let store = self.store.clone();
        let id = p.id.clone();
        let row = tokio::task::spawn_blocking(move || -> Result<MatrixRow> {
            let mut row = store
                .get(&id)?
                .ok_or_else(|| anyhow::anyhow!("not found: {id}"))?;
            row.status = "hot".to_string();
            row.checkout_nonce = Some(nonce_c);
            row.updated_at = Utc::now();
            store.insert(&row)?;
            Ok(row)
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;

        self.stats.lock().unwrap().checkouts += 1;
        let fenced = fence_body(&nonce, &row, &content);
        Ok(format!(
            "{}checkout nonce: {nonce}\n{}\n\n{fenced}",
            warning.unwrap_or_default(),
            fmt_row(&row)
        ))
    }

    /// Return a checked-out entry. Requires the nonce from checkout. Marks entry cold.
    #[tool]
    async fn checkin(&self, Parameters(p): Parameters<CheckinParams>) -> Result<String, String> {
        let store = self.store.clone();
        let id = p.id.clone();
        let nonce = p.nonce.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut row = store
                .get(&id)?
                .ok_or_else(|| anyhow::anyhow!("not found: {id}"))?;
            match &row.checkout_nonce {
                Some(n) if n == &nonce => {}
                Some(_) => anyhow::bail!("nonce mismatch — not the checkout holder"),
                None => anyhow::bail!("entry is not checked out"),
            }
            row.status = "cold".to_string();
            row.checkout_nonce = None;
            row.updated_at = Utc::now();
            store.insert(&row)
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;

        self.checkin_times
            .lock()
            .unwrap()
            .insert(p.id.clone(), Instant::now());
        self.stats.lock().unwrap().checkins += 1;
        Ok(format!("checked in: {}", p.id))
    }

    /// Pin an entry — sets salience 1.0, status pinned. Pinned entries survive cleanup.
    #[tool]
    async fn pin(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let store = self.store.clone();
        let id = p.id.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut row = store
                .get(&id)?
                .ok_or_else(|| anyhow::anyhow!("not found: {id}"))?;
            row.status = "pinned".to_string();
            row.salience = 1.0;
            row.updated_at = Utc::now();
            store.insert(&row)
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
        Ok(format!("pinned: {}", p.id))
    }

    /// Permanently delete an entry and its body file.
    #[tool]
    async fn forget(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let body_path = self.store.body_path(&p.id);
        if body_path.exists() {
            tokio::fs::remove_file(&body_path)
                .await
                .map_err(|e| e.to_string())?;
        }
        let store = self.store.clone();
        let id = p.id.clone();
        let deleted = tokio::task::spawn_blocking(move || store.remove(&id))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;
        if deleted {
            Ok(format!("forgotten: {}", p.id))
        } else {
            Err(format!("not found: {}", p.id))
        }
    }

    /// Delete stale cold entries by age and/or salience. Pinned entries are always spared.
    /// Requires at least one filter. Use dry_run:true to preview before deleting.
    #[tool]
    async fn reap(&self, Parameters(p): Parameters<ReapParams>) -> Result<String, String> {
        if p.max_age_days.is_none() && p.max_salience.is_none() {
            return Err("reap requires at least one of max_age_days or max_salience".to_string());
        }

        let store = self.store.clone();
        let rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        let status_filter = p.status.as_deref().unwrap_or("cold");
        let now = Utc::now();

        let targets: Vec<MatrixRow> = rows
            .into_iter()
            .filter(|r| {
                if r.status == "pinned" {
                    return false;
                }
                if r.status != status_filter {
                    return false;
                }
                let age_ok = p.max_age_days.map_or(true, |days| {
                    (now - r.created_at).num_days() >= days as i64
                });
                let sal_ok = p.max_salience.map_or(true, |max| r.salience <= max);
                age_ok && sal_ok
            })
            .collect();

        if targets.is_empty() {
            return Ok("nothing to reap".to_string());
        }

        let preview = targets
            .iter()
            .map(|r| fmt_row(r))
            .collect::<Vec<_>>()
            .join("\n");

        if p.dry_run {
            return Ok(format!(
                "dry run — would reap {} entries:\n{}",
                targets.len(),
                preview
            ));
        }

        let count = targets.len();
        for row in &targets {
            let body_path = self.store.body_path(&row.id);
            if body_path.exists() {
                tokio::fs::remove_file(&body_path)
                    .await
                    .map_err(|e| e.to_string())?;
            }
            let store = self.store.clone();
            let id_c = row.id.clone();
            tokio::task::spawn_blocking(move || store.remove(&id_c))
                .await
                .map_err(|e| e.to_string())?
                .map_err(|e| e.to_string())?;
        }

        Ok(format!("reaped {count} entries:\n{preview}"))
    }

    /// Full matrix sorted by salience. Use peek for a cheaper count-only snapshot.
    #[tool]
    async fn matrix(&self) -> Result<String, String> {
        let store = self.store.clone();
        let mut rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        self.stats.lock().unwrap().matrix_calls += 1;

        if rows.is_empty() {
            return Ok("hydra is empty".to_string());
        }
        rows.sort_by(|a, b| {
            b.salience
                .partial_cmp(&a.salience)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let total_tokens: u32 = rows.iter().map(|r| r.token_estimate).sum();
        let pinned = rows.iter().filter(|r| r.status == "pinned").count();
        let header = format!(
            "{} entries | ~{} tok banked | {} pinned",
            rows.len(),
            total_tokens,
            pinned
        );
        let body = rows.iter().map(|r| fmt_row(r)).collect::<Vec<_>>().join("\n");
        Ok(format!("{header}\n{body}"))
    }

    /// Count-only snapshot: entries by status and type. Cheaper than matrix.
    #[tool]
    async fn peek(&self) -> Result<String, String> {
        let store = self.store.clone();
        let rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        self.stats.lock().unwrap().peek_calls += 1;

        if rows.is_empty() {
            return Ok("hydra: empty".to_string());
        }
        let cold = rows.iter().filter(|r| r.status == "cold").count();
        let hot = rows.iter().filter(|r| r.status == "hot").count();
        let pinned = rows.iter().filter(|r| r.status == "pinned").count();
        let total_bytes: u64 = rows.iter().map(|r| r.token_estimate as u64 * 4).sum();

        let mut type_counts: HashMap<&str, usize> = HashMap::new();
        for r in &rows {
            *type_counts.entry(r.ctx_type.as_str()).or_insert(0) += 1;
        }
        let mut type_parts: Vec<String> = type_counts
            .iter()
            .map(|(k, v)| format!("{k}({v})"))
            .collect();
        type_parts.sort();

        Ok(format!(
            "{} entries | cold:{cold} hot:{hot} pinned:{pinned} | ~{total_bytes}B\ntypes: {}",
            rows.len(),
            type_parts.join(" ")
        ))
    }

    /// Session operation counts and content volume. No token-savings claims — server
    /// cannot observe the context window. Use for behavioral diagnostics only.
    #[tool]
    async fn stats(&self) -> Result<String, String> {
        let s = self.stats.lock().unwrap();
        Ok(format!(
            "context-hydra session\n\
             offloads  by-value:{} (~{}B)  by-ref:{} (~{}B)  dedup-hits:{}\n\
             recalls:{}  checkouts:{}  checkins:{}\n\
             matrix:{}  peek:{}  search:{}",
            s.offloads,
            s.bytes_offloaded,
            s.ref_offloads,
            s.bytes_ref_offloaded,
            s.dedup_hits,
            s.recalls,
            s.checkouts,
            s.checkins,
            s.matrix_calls,
            s.peek_calls,
            s.searches,
        ))
    }
}

// ---------- private helpers ----------

impl HydraServer {
    async fn do_offload(
        &self,
        content: String,
        ctx_type: String,
        topic: String,
        summary: String,
        tags: Vec<String>,
        salience: f64,
        pin: bool,
        by_ref: bool,
        bytes: u64,
    ) -> Result<(String, String), String> {
        let hash = content_hash(&content);

        let store2 = self.store.clone();
        let hash_c = hash.clone();
        let existing = tokio::task::spawn_blocking(move || store2.find_by_hash(&hash_c))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;
        if let Some(mut existing_row) = existing {
            // If pin requested and entry is not already pinned, pin it now.
            if pin && existing_row.status != "pinned" {
                existing_row.status = "pinned".to_string();
                existing_row.salience = 1.0;
                existing_row.updated_at = Utc::now();
                let store2 = self.store.clone();
                let row_c = existing_row.clone();
                tokio::task::spawn_blocking(move || store2.insert(&row_c))
                    .await
                    .map_err(|e| e.to_string())?
                    .map_err(|e| e.to_string())?;
            }
            self.stats.lock().unwrap().dedup_hits += 1;
            let id = existing_row.id.clone();
            return Ok((id, format!("duplicate: content already banked\n{}", fmt_row(&existing_row))));
        }

        let (status, final_salience) = if pin {
            ("pinned".to_string(), 1.0f64)
        } else {
            ("cold".to_string(), salience)
        };

        let id = uuid::Uuid::new_v4().to_string();
        let row = MatrixRow {
            token_estimate: estimate_tokens(&content),
            id: id.clone(),
            ctx_type,
            topic,
            tags,
            summary,
            salience: final_salience,
            status,
            created_at: Utc::now(),
            updated_at: Utc::now(),
            checkout_nonce: None,
            content_hash: hash,
        };

        let body_path = self.store.body_path(&id);
        tokio::fs::write(&body_path, &content)
            .await
            .map_err(|e| e.to_string())?;

        let store = self.store.clone();
        let row_c = row.clone();
        tokio::task::spawn_blocking(move || store.insert(&row_c))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        {
            let mut s = self.stats.lock().unwrap();
            if by_ref {
                s.ref_offloads += 1;
                s.bytes_ref_offloaded += bytes;
            } else {
                s.offloads += 1;
                s.bytes_offloaded += bytes;
            }
        }

        Ok((id, format!("offloaded\n{}", fmt_row(&row))))
    }

    async fn read_matrix_resource(&self) -> ResourceContents {
        let store = self.store.clone();
        let text = tokio::task::spawn_blocking(move || -> String {
            let Ok(mut rows) = store.all() else {
                return "error reading matrix".to_string();
            };
            if rows.is_empty() {
                return "hydra is empty".to_string();
            }
            rows.sort_by(|a, b| {
                b.salience
                    .partial_cmp(&a.salience)
                    .unwrap_or(std::cmp::Ordering::Equal)
            });
            let total: u32 = rows.iter().map(|r| r.token_estimate).sum();
            let pinned = rows.iter().filter(|r| r.status == "pinned").count();
            let header = format!(
                "{} entries | ~{} tok banked | {} pinned",
                rows.len(),
                total,
                pinned
            );
            let body = rows.iter().map(|r| fmt_row(r)).collect::<Vec<_>>().join("\n");
            format!("{header}\n{body}")
        })
        .await
        .unwrap_or_else(|_| "error".to_string());

        ResourceContents::text(text, "hydra://matrix")
    }

    async fn read_body_resource(&self, id: &str) -> Result<ResourceContents, String> {
        let body_path = self.store.body_path(id);
        if !body_path.exists() {
            return Err(format!("not found: {id}"));
        }
        let content = tokio::fs::read_to_string(&body_path)
            .await
            .map_err(|e| e.to_string())?;

        let store = self.store.clone();
        let id_c = id.to_string();
        let row = tokio::task::spawn_blocking(move || store.get(&id_c))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        let nonce = short_nonce();
        let uri = format!("hydra://body/{id}");
        let text = match row {
            Some(r) => fence_body(&nonce, &r, &content),
            None => content,
        };
        Ok(ResourceContents::text(text, uri))
    }
}

// ---------- server handler (resources) ----------

#[tool_handler]
impl ServerHandler for HydraServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(
            ServerCapabilities::builder()
                .enable_tools()
                .enable_resources()
                .build(),
        )
    }

    fn list_resources(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = Result<ListResourcesResult, ErrorData>> + Send + '_ {
        async {
            Ok(ListResourcesResult::with_all_items(vec![
                Resource::new("hydra://matrix", "matrix")
                    .with_description("Compact index of all offloaded context entries")
                    .with_mime_type("text/plain"),
            ]))
        }
    }

    fn list_resource_templates(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = Result<ListResourceTemplatesResult, ErrorData>>
           + Send
           + '_ {
        async {
            Ok(ListResourceTemplatesResult::with_all_items(vec![
                ResourceTemplate::new("hydra://body/{id}", "body")
                    .with_description("Full body of a hydra entry, fenced as untrusted external data.")
                    .with_mime_type("text/plain"),
            ]))
        }
    }

    fn read_resource(
        &self,
        request: ReadResourceRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> impl std::future::Future<Output = Result<ReadResourceResult, ErrorData>> + Send + '_ {
        async move {
            let uri = &request.uri;
            if uri == "hydra://matrix" {
                let contents = self.read_matrix_resource().await;
                return Ok(ReadResourceResult::new(vec![contents]));
            }
            if let Some(id) = uri.strip_prefix("hydra://body/") {
                return match self.read_body_resource(id).await {
                    Ok(contents) => Ok(ReadResourceResult::new(vec![contents])),
                    Err(e) => Err(ErrorData::invalid_params(e, None)),
                };
            }
            Err(ErrorData::invalid_params(
                format!("unknown resource uri: {uri}"),
                None,
            ))
        }
    }
}
