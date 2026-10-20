//! Bounded stereo PCM transport to an explicitly selected native device. The callback never locks.
use axum::{body::Bytes, extract::Path, http::{HeaderMap, StatusCode}, response::{IntoResponse, Response}, Json};
use cpal::{traits::{DeviceTrait, HostTrait, StreamTrait}, SampleFormat};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{sync::{atomic::{AtomicBool, AtomicU64, AtomicU32, Ordering}, Arc, LazyLock, Mutex, mpsc}, time::{Duration, Instant}};

struct Queue {
    samples: Box<[AtomicU32]>,
    read: AtomicU64,
    write: AtomicU64,
    played: AtomicU64,
    underruns: AtomicU64,
    started: AtomicBool,
    failed: AtomicBool,
    closed: AtomicBool,
    heartbeat: AtomicU64,
    epoch: Instant,
    target: usize,
    block: usize,
    rate: u32,
    device: String,
    device_id: String,
}
impl Queue {
    fn touch(&self) { self.heartbeat.store(self.epoch.elapsed().as_millis() as u64, Ordering::Relaxed); }
    fn queued(&self) -> usize {
        let read = self.read.load(Ordering::Acquire);
        self.write.load(Ordering::Acquire).saturating_sub(read) as usize / 2
    }
    fn status(&self, id: &str) -> serde_json::Value {
        let mut value = json!({"id":id,"device":self.device,"device_id":self.device_id,"sample_rate":self.rate,
            "queued_frames":self.queued(),"played_frames":self.played.load(Ordering::Relaxed),
            "underruns":self.underruns.load(Ordering::Relaxed),"started":self.started.load(Ordering::Relaxed)});
        if self.failed.load(Ordering::Relaxed) { value["error"] = json!("native output device failed"); }
        else if self.closed.load(Ordering::Acquire) { value["error"] = json!("native output closed or expired"); }
        value
    }
}
struct Session {
    id: String,
    queue: Arc<Queue>,
    sequence: Mutex<u64>,
    stop: mpsc::Sender<()>,
    done: Mutex<mpsc::Receiver<()>>,
}
static ACTIVE: LazyLock<Mutex<Option<Arc<Session>>>> = LazyLock::new(|| Mutex::new(None));
#[derive(Deserialize)]
pub struct OpenReq { sample_rate: u32, block_frames: usize, target_blocks: usize, device_id: Option<String> }
fn error(status: StatusCode, code: &str, detail: impl ToString) -> Response {
    (status, Json(json!({"error":code,"detail":detail.to_string()}))).into_response()
}

fn device_id(name: &str) -> String { format!("{:x}", Sha256::digest(name.as_bytes())) }

pub async fn devices() -> Response {
    let host = cpal::default_host();
    let outputs = match host.output_devices() {
        Ok(outputs) => outputs,
        Err(e) => return error(StatusCode::SERVICE_UNAVAILABLE, "OUTPUT_UNAVAILABLE", e),
    };
    let names: Vec<_> = outputs.filter_map(|d| d.name().ok().map(|name| (name, d))).collect();
    let default_name = host.default_output_device().and_then(|d| d.name().ok());
    let items: Vec<_> = names.iter().map(|(name, device)| {
        let ambiguous = names.iter().filter(|(n, _)| n == name).count() != 1;
        let mut item = json!({"id":device_id(name),"name":name,"is_default":default_name.as_ref()==Some(name),"selectable":false});
        match device.default_output_config() {
            Ok(config) => {
                let supported = config.channels()==2 && matches!(config.sample_format(), SampleFormat::F32 | SampleFormat::I16 | SampleFormat::U16);
                item["sample_rate"] = json!(config.sample_rate().0);
                item["channels"] = json!(config.channels());
                item["selectable"] = json!(supported && !ambiguous);
                if ambiguous { item["reason"] = json!("同名设备无法唯一识别"); }
                else if !supported { item["reason"] = json!("需要双声道及受支持的PCM格式"); }
            }
            Err(e) => { item["reason"] = json!(e.to_string()); }
        }
        item
    }).collect();
    Json(json!({"devices":items})).into_response()
}

fn select_device(id: Option<&str>) -> Result<cpal::Device, String> {
    let host = cpal::default_host();
    let Some(id) = id else { return host.default_output_device().ok_or("no default output device".into()); };
    let mut matches = host.output_devices().map_err(|e| e.to_string())?
        .filter(|device| device.name().ok().is_some_and(|name| device_id(&name)==id));
    let device = matches.next().ok_or("selected output device is unavailable; refresh devices")?;
    if matches.next().is_some() { return Err("selected device name is ambiguous; rename devices in Windows settings".into()); }
    Ok(device)
}

// Single producer (serialized outside callback), single consumer. Publish the entire
// block with one release store so a callback can never observe half a stereo frame.
fn fill<T: Copy>(out: &mut [T], q: &Queue, convert: impl Fn(f32) -> T) {
    let mut read = q.read.load(Ordering::Relaxed);
    let write = q.write.load(Ordering::Acquire);
    let available = (write - read) as usize;
    let running = q.started.load(Ordering::Relaxed) || available >= q.target * 2;
    let count = if running { available.min(out.len()) / 2 * 2 } else { 0 };
    for (i, sample) in out.iter_mut().enumerate() {
        *sample = if i < count {
            let value = f32::from_bits(q.samples[read as usize % q.samples.len()].load(Ordering::Relaxed));
            read += 1;
            convert(value)
        } else { convert(0.0) };
    }
    if count > 0 {
        q.read.store(read, Ordering::Release);
        q.played.fetch_add((count / 2) as u64, Ordering::Relaxed);
    }
    if running && count < out.len() {
        q.underruns.fetch_add(1, Ordering::Relaxed);
        q.started.store(false, Ordering::Relaxed);
    } else if running { q.started.store(true, Ordering::Relaxed); }
}
fn create_stream(rate: u32, block: usize, target: usize, capacity: usize, selected: Option<&str>) -> Result<(cpal::Stream, Arc<Queue>), String> {
    let device = select_device(selected)?;
    let supported = device.default_output_config().map_err(|e| e.to_string())?;
    if supported.channels() != 2 || supported.sample_rate().0 != rate {
        return Err(format!("requested stereo {rate} Hz; selected device is {} channels / {} Hz", supported.channels(), supported.sample_rate().0));
    }
    let format = supported.sample_format();
    let config = supported.into();
    let queue = Arc::new(Queue {
        samples: (0..capacity * 2).map(|_| AtomicU32::new(0)).collect(),
        read: AtomicU64::new(0), write: AtomicU64::new(0), played: AtomicU64::new(0), underruns: AtomicU64::new(0),
        started: AtomicBool::new(false), failed: AtomicBool::new(false), closed: AtomicBool::new(false),
        heartbeat: AtomicU64::new(0), epoch: Instant::now(), target, block, rate,
        device: device.name().map_err(|e| e.to_string())?,
        device_id: device_id(&device.name().map_err(|e| e.to_string())?),
    });
    let q = queue.clone();
    let failed = queue.clone();
    let on_error = move |_| { failed.failed.store(true, Ordering::Relaxed); };
    let stream = match format {
        SampleFormat::F32 => device.build_output_stream(&config, move |o: &mut [f32], _| fill(o, &q, |v| v), on_error, None),
        SampleFormat::I16 => device.build_output_stream(&config, move |o: &mut [i16], _| fill(o, &q, |v| (v.clamp(-1.0, 1.0) * i16::MAX as f32) as i16), on_error, None),
        SampleFormat::U16 => device.build_output_stream(&config, move |o: &mut [u16], _| fill(o, &q, |v| ((v.clamp(-1.0, 1.0) + 1.0) * 32767.5) as u16), on_error, None),
        _ => return Err("selected device sample format is unsupported".into()),
    }.map_err(|e| e.to_string())?;
    stream.play().map_err(|e| e.to_string())?;
    Ok((stream, queue))
}
fn stop(session: &Session) -> Result<(), String> {
    let _ = session.stop.send(());
    session.done.lock().unwrap().recv_timeout(Duration::from_secs(3)).map_err(|_| "device close timed out".to_string())
}
pub async fn open(Json(req): Json<OpenReq>) -> Response {
    if !(8000..=192000).contains(&req.sample_rate) || !(1..=8192).contains(&req.block_frames) || !(1..=128).contains(&req.target_blocks) {
        return error(StatusCode::BAD_REQUEST, "INVALID_AUDIO_CONFIG", "invalid sample rate or buffer settings");
    }
    let capacity = req.block_frames * (req.target_blocks + 3);
    if capacity > 1_048_576 { return error(StatusCode::BAD_REQUEST, "INVALID_AUDIO_CONFIG", "queue too large"); }
    let mut active = ACTIVE.lock().unwrap();
    if active.as_ref().is_some_and(|s| s.queue.closed.load(Ordering::Acquire)) { active.take(); }
    if active.is_some() { return error(StatusCode::CONFLICT, "OUTPUT_BUSY", "a native output stream is already open"); }
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let (stop_tx, stop_rx) = mpsc::channel();
    let (done_tx, done_rx) = mpsc::channel();
    let target = req.block_frames * req.target_blocks;
    let owner = std::thread::Builder::new().name("svs-device-output".into()).spawn(move || {
        match create_stream(req.sample_rate, req.block_frames, target, capacity, req.device_id.as_deref()) {
            Ok((stream, queue)) => {
                if ready_tx.send(Ok(queue.clone())).is_ok() {
                    loop {
                        if stop_rx.recv_timeout(Duration::from_millis(100)) != Err(mpsc::RecvTimeoutError::Timeout) { break; }
                        let idle = (queue.epoch.elapsed().as_millis() as u64).saturating_sub(queue.heartbeat.load(Ordering::Relaxed));
                        if idle > 5000 || queue.failed.load(Ordering::Relaxed) { break; }
                    }
                }
                drop(stream);
                queue.closed.store(true, Ordering::Release);
            }
            Err(e) => { let _ = ready_tx.send(Err(e)); }
        }
        let _ = done_tx.send(());
    });
    if let Err(e) = owner { return error(StatusCode::SERVICE_UNAVAILABLE, "OUTPUT_UNAVAILABLE", e); }
    let queue = match ready_rx.recv_timeout(Duration::from_secs(8)) {
        Ok(Ok(queue)) => queue,
        Ok(Err(e)) => return error(StatusCode::SERVICE_UNAVAILABLE, "OUTPUT_UNAVAILABLE", e),
        Err(_) => { let _ = stop_tx.send(()); return error(StatusCode::SERVICE_UNAVAILABLE, "OUTPUT_UNAVAILABLE", "device initialization timed out"); }
    };
    let session = Arc::new(Session { id: uuid::Uuid::new_v4().to_string(), queue, sequence: Mutex::new(0), stop: stop_tx, done: Mutex::new(done_rx) });
    let response = json!({"id":session.id,"sample_rate":session.queue.rate,"channels":2,"device":session.queue.device,"device_id":session.queue.device_id,"capacity_frames":capacity,"target_frames":target});
    *active = Some(session);
    Json(response).into_response()
}
fn session(id: &str) -> Option<Arc<Session>> { ACTIVE.lock().unwrap().as_ref().filter(|s| s.id == id).cloned() }
pub async fn pcm(Path(id): Path<String>, headers: HeaderMap, body: Bytes) -> Response {
    let Some(s) = session(&id) else { return error(StatusCode::NOT_FOUND, "OUTPUT_NOT_FOUND", "unknown output stream"); };
    let q = &s.queue;
    if q.closed.load(Ordering::Acquire) || q.failed.load(Ordering::Relaxed) { return error(StatusCode::GONE, "OUTPUT_CLOSED", "device stream closed or failed"); }
    let Some(sequence) = headers.get("x-audio-sequence").and_then(|h| h.to_str().ok()).and_then(|h| h.parse::<u64>().ok()) else { return error(StatusCode::BAD_REQUEST, "INVALID_SEQUENCE", "x-audio-sequence required"); };
    if body.len() != q.block * 8 { return error(StatusCode::BAD_REQUEST, "INVALID_PCM", "exactly one stereo f32le block required"); }
    if body.chunks_exact(4).any(|v| !f32::from_le_bytes(v.try_into().unwrap()).is_finite()) { return error(StatusCode::BAD_REQUEST, "INVALID_PCM", "samples must be finite"); }
    let mut next = s.sequence.lock().unwrap();
    if sequence != *next { return error(StatusCode::CONFLICT, "SEQUENCE_MISMATCH", format!("expected {}", *next)); }
    let write = q.write.load(Ordering::Relaxed);
    let used = write - q.read.load(Ordering::Acquire);
    if q.samples.len() - (used as usize) < body.len() / 4 { return error(StatusCode::CONFLICT, "OUTPUT_FULL", "device queue full"); }
    for (i, value) in body.chunks_exact(4).enumerate() {
        q.samples[(write as usize + i) % q.samples.len()].store(u32::from_le_bytes(value.try_into().unwrap()), Ordering::Relaxed);
    }
    q.write.store(write + (body.len() / 4) as u64, Ordering::Release);
    *next += 1;
    q.touch();
    Json(q.status(&id)).into_response()
}
pub async fn status(Path(id): Path<String>) -> Response {
    let Some(s) = session(&id) else { return error(StatusCode::NOT_FOUND, "OUTPUT_NOT_FOUND", "unknown output stream"); };
    s.queue.touch();
    Json(s.queue.status(&id)).into_response()
}
pub async fn close(Path(id): Path<String>) -> Response {
    let mut active = ACTIVE.lock().unwrap();
    if let Some(s) = active.as_ref().filter(|s| s.id == id) {
        if let Err(e) = stop(s) { return error(StatusCode::SERVICE_UNAVAILABLE, "OUTPUT_CLOSE_FAILED", e); }
        active.take();
    }
    StatusCode::NO_CONTENT.into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn callback_preserves_stereo_order_and_holds_clock_while_refilling() {
        let q = Queue {
            samples: (0..16).map(|_| AtomicU32::new(0)).collect(),
            read: AtomicU64::new(0), write: AtomicU64::new(0), played: AtomicU64::new(0),
            underruns: AtomicU64::new(0), started: AtomicBool::new(false), failed: AtomicBool::new(false),
            closed: AtomicBool::new(false), heartbeat: AtomicU64::new(0), epoch: Instant::now(),
            target: 2, block: 2, rate: 48000, device: String::new(), device_id: String::new(),
        };
        let push = |samples: &[f32]| {
            let write = q.write.load(Ordering::Relaxed);
            for (i, value) in samples.iter().enumerate() {
                q.samples[(write as usize + i) % q.samples.len()].store(value.to_bits(), Ordering::Relaxed);
            }
            q.write.store(write + samples.len() as u64, Ordering::Release);
        };
        push(&[0.25, -0.25]);
        let mut output = [9.0_f32; 6];
        fill(&mut output, &q, |v| v);
        assert_eq!(output, [0.0; 6]);
        assert_eq!(q.played.load(Ordering::Relaxed), 0);
        push(&[0.5, -0.5]);
        fill(&mut output, &q, |v| v);
        assert_eq!(output, [0.25, -0.25, 0.5, -0.5, 0.0, 0.0]);
        assert_eq!(q.played.load(Ordering::Relaxed), 2);
        assert_eq!(q.underruns.load(Ordering::Relaxed), 1);
        assert!(!q.started.load(Ordering::Relaxed));
        push(&[0.75, -0.75]);
        fill(&mut output, &q, |v| v);
        assert_eq!(output, [0.0; 6]);
        assert_eq!(q.played.load(Ordering::Relaxed), 2);
        push(&[1.0, -1.0]);
        fill(&mut output[..4], &q, |v| v);
        assert_eq!(&output[..4], &[0.75, -0.75, 1.0, -1.0]);
        assert_eq!(q.played.load(Ordering::Relaxed), 4);
    }
}
