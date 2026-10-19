//! SVS server：工程状态 + REST API + WS 推送 + 推理调度 + WAV 并轨。
//!
//! 拉起 inference-service 子进程；按契约 §4/§5 调度切片合成；
//! REST 语义与 CLI（契约 §7）一一对应。

mod engine;
mod handlers;
mod state;
mod playback;
mod extensions;
mod tempo;
mod media;
mod expression;
mod interchange;
mod mixing;
mod rendering;
mod stems;

use axum::routing::{get, patch, post};
use axum::Router;
use std::net::SocketAddr;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let st = state::AppState::new();

    // 拉起推理服务（失败不阻塞：合成时按 503 报告）。
    // --no-inference：不拉起子进程（轻量使用：只要乐谱编辑/MIDI/工程管理时用，
    //   避免后台出现 python 推理进程；需要人声合成时去掉本参数重启即可）。
    let no_inference = std::env::args().any(|a| a == "--no-inference");
    if no_inference {
        println!("[svs-server] --no-inference：跳过推理服务拉起（人声合成将不可用）");
    } else {
        engine::ensure_inference(st.clone()).await;
    }

    let app = Router::new()
        .route("/api/project", get(handlers::get_project))
        .route("/api/project/new", post(handlers::new_project))
        .route("/api/project/open", post(handlers::open_project))
        .route("/api/project/save", post(handlers::save_project))
        .route("/api/project/tempos", patch(tempo::patch_tempos))
        .route("/api/project/master", patch(handlers::patch_master))
        .route("/api/project/mixer", patch(mixing::patch_mixer))
        .route("/api/track", post(handlers::add_track))
        .route("/api/track/{id}", patch(handlers::patch_track).delete(handlers::del_track))
        .route("/api/track/{id}/note", post(handlers::add_note))
        .route("/api/track/{id}/notes", get(handlers::list_notes))
        .route("/api/track/{id}/note/{nid}", patch(handlers::patch_note).delete(handlers::del_note))
        .route("/api/track/{id}/curve", post(handlers::paint_curve))
        .route("/api/track/{id}/curve/{param}", get(handlers::get_curve))
        .route("/api/track/{id}/expression", post(expression::update_track_expression))
        .route("/api/synth", post(handlers::synth))
        .route("/api/playback/plan", get(playback::plan))
        .route("/api/playback/phrase", post(playback::phrase))
        .route("/api/transport/{action}", post(handlers::transport))
        .route("/api/transport/seek", post(handlers::transport_seek))
        .route("/api/render", post(handlers::render_wav))
        .route("/api/render/stems", post(stems::export_stems))
        .route("/api/import/abc", post(handlers::import_abc))
        .route("/api/ai/lyrics", post(handlers::ai_lyrics))
        .route("/api/interchange", post(interchange::interchange))
        .route("/api/media/probe", post(media::probe_media))
        .route("/api/media/import", post(media::import_audio))
        .route("/api/media/export/fcpxml", post(media::export_fcpxml))
        .route("/api/media/export/aaf", post(media::export_aaf))
        .route("/api/media/assemble_video", post(media::assemble_video))
        .route("/api/media/clip/{id}/pcm", get(media::clip_pcm))
        .route("/api/inference/{*path}", axum::routing::any(extensions::proxy))
        .layer(axum::extract::DefaultBodyLimit::max(140 * 1024 * 1024))
        .route("/api/pcm", get(handlers::get_pcm))
        .route("/api/health", get(handlers::health))
        .route("/ws", get(handlers::ws_handler))
        .fallback_service(tower_http::services::ServeDir::new(std::env::var("SVS_DESKTOP_DIST").unwrap_or_else(|_| concat!(env!("CARGO_MANIFEST_DIR"), "/../desktop/dist").to_string()))
            .append_index_html_on_directories(true))
        .with_state(st);

    let addr = SocketAddr::from(([127, 0, 0, 1], 8761));
    println!("[svs-server] listening on http://{addr}");
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;
    Ok(())
}
