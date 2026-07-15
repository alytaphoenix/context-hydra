use std::sync::Arc;

use anyhow::Result;
use chrono::Utc;
use rmcp::{handler::server::wrapper::Parameters, tool, tool_router};
use schemars::JsonSchema;
use serde::Deserialize;

use crate::store::{MatrixRow, Store};

pub struct HydraServer {
    store: Arc<Store>,
}

impl HydraServer {
    pub fn new() -> Result<Self> {
        Ok(Self {
            store: Arc::new(Store::new()?),
        })
    }
}

fn estimate_tokens(text: &str) -> u32 {
    (text.len() as u32 + 3) / 4
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

fn default_salience() -> f64 {
    0.5
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

#[tool_router(server_handler)]
impl HydraServer {
    /// Offload content out of the active context window into the hydra store.
    /// Returns a compact matrix row pointer. The full content is preserved externally
    /// and can be retrieved with `hydrate`. Use this to keep context lean.
    #[tool]
    async fn offload(
        &self,
        Parameters(p): Parameters<OffloadParams>,
    ) -> Result<String, String> {
        let id = uuid::Uuid::new_v4().to_string();
        let row = MatrixRow {
            token_estimate: estimate_tokens(&p.content),
            id: id.clone(),
            ctx_type: p.ctx_type,
            topic: p.topic,
            tags: p.tags,
            summary: p.summary,
            salience: p.salience,
            status: "cold".to_string(),
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };

        let body_path = self.store.body_path(&id);
        tokio::fs::write(&body_path, p.content)
            .await
            .map_err(|e| e.to_string())?;

        let store = self.store.clone();
        let row_c = row.clone();
        tokio::task::spawn_blocking(move || store.insert(&row_c))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        Ok(format!("offloaded\n{}", fmt_row(&row)))
    }

    /// Search the matrix by keyword, context type, status, or minimum salience.
    /// Returns compact matrix rows — use `hydrate` to pull full content of a match.
    #[tool]
    async fn search(
        &self,
        Parameters(p): Parameters<SearchParams>,
    ) -> Result<String, String> {
        let store = self.store.clone();
        let rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

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
        Ok(filtered.iter().map(|r| fmt_row(r)).collect::<Vec<_>>().join("\n"))
    }

    /// Retrieve the full content body of a stored entry by id, pulling it back into active context.
    #[tool]
    async fn hydrate(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let body_path = self.store.body_path(&p.id);
        if !body_path.exists() {
            return Err(format!("not found: {}", p.id));
        }
        let content = tokio::fs::read_to_string(&body_path)
            .await
            .map_err(|e| e.to_string())?;

        let store = self.store.clone();
        let id = p.id.clone();
        let row = tokio::task::spawn_blocking(move || store.get(&id))
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

        Ok(match row {
            Some(r) => format!("[hydrated: {}]\n{}\n\n{}", r.topic, fmt_row(&r), content),
            None => content,
        })
    }

    /// Mark a context entry as cold without deleting it.
    /// Signals that the content is no longer needed in active context but should be kept.
    #[tool]
    async fn evict(&self, Parameters(p): Parameters<IdParam>) -> Result<String, String> {
        let store = self.store.clone();
        let id = p.id.clone();
        tokio::task::spawn_blocking(move || -> Result<()> {
            let mut row = store
                .get(&id)?
                .ok_or_else(|| anyhow::anyhow!("not found: {id}"))?;
            row.status = "cold".to_string();
            row.updated_at = Utc::now();
            store.insert(&row)
        })
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())?;
        Ok(format!("evicted: {}", p.id))
    }

    /// Pin a context entry — marks it always-relevant, sets salience to 1.0, status to pinned.
    /// Pinned entries appear first in `matrix` and are preserved through cleanup passes.
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

    /// Permanently delete a context entry and its content body. Cannot be undone.
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

    /// Show the full context matrix — all stored entries as compact rows sorted by salience.
    /// Low-token view of everything offloaded to the hydra. Use this to orient before searching or hydrating.
    #[tool]
    async fn matrix(&self) -> Result<String, String> {
        let store = self.store.clone();
        let mut rows = tokio::task::spawn_blocking(move || store.all())
            .await
            .map_err(|e| e.to_string())?
            .map_err(|e| e.to_string())?;

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
            "{} entries | ~{} tokens offloaded | {} pinned",
            rows.len(),
            total_tokens,
            pinned
        );
        let body = rows.iter().map(|r| fmt_row(r)).collect::<Vec<_>>().join("\n");
        Ok(format!("{header}\n{body}"))
    }
}
