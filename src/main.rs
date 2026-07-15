mod server;
mod store;

use rmcp::{serve_server, transport::stdio};
use server::HydraServer;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("warn")),
        )
        .init();

    let server = HydraServer::new()?;
    let service = serve_server(server, stdio()).await?;
    service.waiting().await?;
    Ok(())
}
