//! 全局状态：工程 + 历史栈 + 推理服务句柄 + WS 广播。

use svs_core::history::History;
use svs_core::project::Project;
use std::sync::Arc;
use tokio::sync::{broadcast, Mutex, RwLock};

#[derive(Clone)]
pub struct AppState {
    pub project: Arc<RwLock<Project>>,
    pub history: Arc<Mutex<History<Project>>>,
    pub path: Arc<RwLock<Option<String>>>,
    pub inference_url: Arc<RwLock<Option<String>>>,
    pub ws_tx: broadcast::Sender<String>,
    pub transport: Arc<Mutex<TransportState>>,
    pub http: reqwest::Client,
    /// 最近一次离线渲染的混音结果（/api/pcm 导出检查用，试听不依赖）。
    pub last_mix: Arc<RwLock<Option<Vec<f32>>>>,
    /// 工程编辑版本号：每次 mutation 成功后自增。
    pub edit_rev: Arc<std::sync::atomic::AtomicU64>,
    /// 声音内容版本；仅混音控件变化不使已合成短句失效。
    pub audio_rev: Arc<std::sync::atomic::AtomicU64>,
    /// last_mix 生成时的 edit_rev；不一致即混音过期。
    pub mix_rev: Arc<RwLock<u64>>,
    /// 合成、试听及导出共用的推理闸，避免并发模型调用占满算力。
    pub render_gate: Arc<tokio::sync::Mutex<()>>,
}

#[derive(Default)]
pub struct TransportState {
    pub playing: bool,
    pub start_ms: f64,
    pub start_tick: u32,
    pub started_at: Option<std::time::Instant>,
    /// 拖动播放头时自增；走带任务检测到变化即按新 tick 重建（实时拖动）。
    pub seek_gen: u64,
}

impl AppState {
    pub fn new() -> Self {
        let (tx, _) = broadcast::channel(64);
        Self {
            project: Arc::new(RwLock::new(Project::new("未命名工程"))),
            history: Arc::new(Mutex::new(History::default())),
            path: Arc::new(RwLock::new(None)),
            inference_url: Arc::new(RwLock::new(None)),
            ws_tx: tx,
            transport: Arc::new(Mutex::new(TransportState::default())),
            http: reqwest::Client::new(),
            last_mix: Arc::new(RwLock::new(None)),
            edit_rev: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            audio_rev: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            mix_rev: Arc::new(RwLock::new(u64::MAX)), // 初始不匹配 → 首次播放必触发渲染
            render_gate: Arc::new(tokio::sync::Mutex::new(())),
        }
    }

    /// mutation 前快照。
    pub async fn snapshot(&self) {
        let p = self.project.read().await.clone();
        self.history.lock().await.push(&p);
    }

    /// mutation 成功后调用：编辑版本 +1，使已缓存的混音失效。
    pub fn bump_rev(&self) {
        self.edit_rev.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let revision = self.audio_rev.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1;
        let _ = self.ws_tx.send(serde_json::json!({"type": "project.changed", "revision": revision}).to_string());
    }

    /// 混音变更实时应用于浏览器节点，不取消已合成内容。
    pub fn bump_mix_rev(&self) {
        self.edit_rev.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let _ = self.ws_tx.send(serde_json::json!({"type": "project.mixer"}).to_string());
    }

    /// 混音是否与当前编辑状态一致。
    pub async fn mix_fresh(&self) -> bool {
        let cur = self.edit_rev.load(std::sync::atomic::Ordering::SeqCst);
        let made = *self.mix_rev.read().await;
        cur == made
    }

    pub async fn broadcast(&self, msg: serde_json::Value) {
        let _ = self.ws_tx.send(msg.to_string());
    }
}
