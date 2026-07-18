use std::{path::PathBuf, sync::Arc};

use anyhow::Result;
use chrono::{DateTime, Utc};
use redb::{Database, ReadableDatabase, ReadableTable, TableDefinition};
use serde::{Deserialize, Serialize};

// ---------- local model config ----------

#[derive(Debug, Clone, Deserialize)]
pub struct LocalModelConfig {
    pub base_url: String,
    /// Model to use for compression and summarization operations only.
    /// Must have thinking/reasoning mode disabled — see README for guidance.
    /// Accepts `model` as a backwards-compatible alias.
    #[serde(alias = "model")]
    pub compression_model: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_compress_target")]
    pub compress_target_tokens: u32,
    #[serde(default = "default_checkout_remote_target")]
    pub checkout_remote_target_tokens: u32,
}

fn default_compress_target() -> u32 { 200 }
fn default_checkout_remote_target() -> u32 { 300 }

#[derive(Debug, Deserialize)]
struct RootConfig {
    local_model: Option<LocalModelConfig>,
}

/// Read config.toml from `CONTEXT_HYDRA_DATA_DIR` (if set) or the platform data dir.
/// Returns None if the file is absent, unparseable, or `base_url` is empty.
pub fn load_local_model_config() -> Option<LocalModelConfig> {
    let path = if let Ok(dir) = std::env::var("CONTEXT_HYDRA_DATA_DIR") {
        std::path::PathBuf::from(dir).join("config.toml")
    } else {
        dirs::data_dir()?
            .join("context-hydra")
            .join("config.toml")
    };
    let text = std::fs::read_to_string(path).ok()?;
    match toml::from_str::<RootConfig>(&text) {
        Ok(root) => root.local_model.filter(|c| !c.base_url.is_empty()),
        Err(e) => {
            eprintln!("context-hydra: config.toml parse error: {e}");
            None
        }
    }
}

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
    /// Nonce issued on checkout; must be presented to checkin. None when not checked out.
    #[serde(default)]
    pub checkout_nonce: Option<String>,
    /// SHA-256 hex of the content body — used for dedup on offload.
    #[serde(default)]
    pub content_hash: String,
}

pub struct Store {
    pub db: Arc<Database>,
    pub bodies_dir: PathBuf,
}

impl Store {
    pub fn new() -> Result<Self> {
        let data_dir = std::env::var("CONTEXT_HYDRA_DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                dirs::data_dir()
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join("context-hydra")
            });
        std::fs::create_dir_all(&data_dir)?;
        let bodies_dir = data_dir.join("bodies");
        std::fs::create_dir_all(&bodies_dir)?;
        let db = Database::create(data_dir.join("hydra.redb"))?;
        let write_txn = db.begin_write()?;
        write_txn.open_table(MATRIX)?;
        write_txn.commit()?;
        Ok(Self {
            db: Arc::new(db),
            bodies_dir,
        })
    }

    /// On startup: reset any hot entries left over from a previous session.
    /// Stale hot state and orphaned nonces from prior sessions are cleared.
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

    pub fn insert(&self, row: &MatrixRow) -> Result<()> {
        let json = serde_json::to_vec(row)?;
        let write_txn = self.db.begin_write()?;
        {
            let mut table = write_txn.open_table(MATRIX)?;
            table.insert(row.id.as_str(), json.as_slice())?;
        }
        write_txn.commit()?;
        Ok(())
    }

    pub fn get(&self, id: &str) -> Result<Option<MatrixRow>> {
        let read_txn = self.db.begin_read()?;
        let table = read_txn.open_table(MATRIX)?;
        Ok(match table.get(id)? {
            Some(g) => Some(serde_json::from_slice(g.value())?),
            None => None,
        })
    }

    pub fn all(&self) -> Result<Vec<MatrixRow>> {
        let read_txn = self.db.begin_read()?;
        let table = read_txn.open_table(MATRIX)?;
        let mut rows = Vec::new();
        for result in table.iter()? {
            let (_, v) = result?;
            rows.push(serde_json::from_slice(v.value())?);
        }
        Ok(rows)
    }

    pub fn remove(&self, id: &str) -> Result<bool> {
        let write_txn = self.db.begin_write()?;
        let removed = {
            let mut table = write_txn.open_table(MATRIX)?;
            table.remove(id)?.is_some()
        };
        write_txn.commit()?;
        Ok(removed)
    }

    /// Linear scan for a content hash — dedup check on offload.
    pub fn find_by_hash(&self, hash: &str) -> Result<Option<MatrixRow>> {
        if hash.is_empty() {
            return Ok(None);
        }
        let rows = self.all()?;
        Ok(rows.into_iter().find(|r| r.content_hash == hash))
    }

    pub fn body_path(&self, id: &str) -> PathBuf {
        self.bodies_dir.join(format!("{id}.txt"))
    }
}
