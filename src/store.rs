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
        Ok(Self {
            db: Arc::new(db),
            bodies_dir,
        })
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

    pub fn body_path(&self, id: &str) -> PathBuf {
        self.bodies_dir.join(format!("{id}.txt"))
    }
}
