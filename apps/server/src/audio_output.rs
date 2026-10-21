//! Bounded stereo PCM transport to an explicitly selected native device. The render path never locks.
use axum::{body::Bytes, extract::Path, http::{HeaderMap, StatusCode}, response::{IntoResponse, Response}, Json};
use cpal::{traits::{DeviceTrait, HostTrait, StreamTrait}, SampleFormat};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{sync::{atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering}, Arc, LazyLock, Mutex, mpsc}, time::{Duration, Instant}};
#[cfg(not(windows))]
use super::audio_windows::Mode;

pub(super) struct Queue {
    pub(super) samples: Box<[AtomicU32]>, pub(super) read: AtomicU64, pub(super) write: AtomicU64,
    pub(super) played: AtomicU64, pub(super) underruns: AtomicU64, pub(super) started: AtomicBool,
    pub(super) failed: AtomicBool, pub(super) closed: AtomicBool, pub(super) heartbeat: AtomicU64,
    pub(super) epoch: Instant, pub(super) target: usize, pub(super) block: usize, pub(super) rate: u32,
    pub(super) device: Mutex<(String,String)>,
}
impl Queue {
    pub(super) fn touch(&self) { self.heartbeat.store(self.epoch.elapsed().as_millis() as u64, Ordering::Relaxed); }
    fn queued(&self) -> usize { self.write.load(Ordering::Acquire).saturating_sub(self.read.load(Ordering::Acquire)) as usize / 2 }
    fn status(&self, id: &str) -> serde_json::Value {
        let device=self.device.lock().unwrap();
        let mut value=json!({"id":id,"device":device.0,"device_id":device.1,"sample_rate":self.rate,"queued_frames":self.queued(),"played_frames":self.played.load(Ordering::Relaxed),"underruns":self.underruns.load(Ordering::Relaxed),"started":self.started.load(Ordering::Relaxed)});
        if self.failed.load(Ordering::Relaxed) { value["error"]=json!("native output device failed"); }
        else if self.closed.load(Ordering::Acquire) { value["error"]=json!("native output closed or expired"); }
        value
    }
}
struct Session {
    id:String, queue:Arc<Queue>, sequence:Mutex<u64>, stop:mpsc::Sender<()>, done:Mutex<mpsc::Receiver<()>>,
    switch:mpsc::Sender<super::audio_windows::Command>, metadata:Arc<Mutex<(String,super::audio_windows::Mode)>>,
}
static ACTIVE:LazyLock<Mutex<Option<Arc<Session>>>>=LazyLock::new(||Mutex::new(None));
static CONTROL:LazyLock<Mutex<()>>=LazyLock::new(||Mutex::new(()));
#[derive(Deserialize)] pub struct OpenReq { sample_rate:u32, block_frames:usize, target_blocks:usize, device_id:Option<String>, mode:Option<String> }
#[derive(Deserialize)] pub struct SwitchReq { device_id:String, sample_rate:u32, mode:String }
fn error(status:StatusCode,code:&str,detail:impl ToString)->Response {(status,Json(json!({"error":code,"detail":detail.to_string()}))).into_response()}
fn device_id(name:&str)->String { format!("{:x}",Sha256::digest(name.as_bytes())) }
fn parse_mode(value:Option<&str>)->Result<super::audio_windows::Mode,String> { match value.unwrap_or("shared") { "shared"=>Ok(super::audio_windows::Mode::Shared),"exclusive"=>Ok(super::audio_windows::Mode::Exclusive),_=>Err("mode must be shared or exclusive".into()) } }

pub async fn devices()->Response {
    match tokio::task::spawn_blocking(devices_blocking).await { Ok(response)=>response,Err(join_error)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",join_error) }
}
fn devices_blocking()->Response {
    #[cfg(windows)] { return match super::audio_windows::endpoints() { Ok(ds)=>Json(json!({"devices":ds.into_iter().map(|d|json!({"id":d.id,"name":d.name,"is_default":d.is_default,"sample_rate":d.sample_rate,"channels":d.channels,"selectable":d.selectable,"reason":d.reason})).collect::<Vec<_>>(),"asio":super::audio_windows::asio_status()})).into_response(),Err(e)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e) }; }
    #[cfg(not(windows))] {
        let host=cpal::default_host(); let outputs=match host.output_devices(){Ok(v)=>v,Err(e)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e)}; let mut items=Vec::new();
        for device in outputs { if let Ok(name)=device.name() { let default=host.default_output_device().and_then(|d|d.name().ok()).as_deref()==Some(&name);let config=device.default_output_config().ok();let selectable=config.as_ref().is_some_and(|c|c.channels()==2&&matches!(c.sample_format(),SampleFormat::F32|SampleFormat::I16|SampleFormat::U16));items.push(json!({"id":device_id(&name),"name":name,"is_default":default,"sample_rate":config.as_ref().map(|c|c.sample_rate().0),"channels":config.as_ref().map(|c|c.channels()),"selectable":selectable})); } }
        Json(json!({"devices":items,"asio":super::audio_windows::asio_status()})).into_response()
    }
}
fn select_device(id:Option<&str>)->Result<cpal::Device,String>{let host=cpal::default_host();let Some(id)=id else{return host.default_output_device().ok_or("no default output device".into())};let mut matches=host.output_devices().map_err(|e|e.to_string())?.filter(|d|d.name().ok().is_some_and(|name|device_id(&name)==id));let device=matches.next().ok_or("selected output device is unavailable")?;if matches.next().is_some(){return Err("selected device name is ambiguous".into())}Ok(device)}

// Publish each complete stereo frame block atomically; callback does no allocation or locking.
pub(super) fn fill<T:Copy>(out:&mut[T],queue:&Queue,convert:impl Fn(f32)->T){
    let read=queue.read.load(Ordering::Relaxed);let write=queue.write.load(Ordering::Acquire);let available=(write-read)as usize;let running=queue.started.load(Ordering::Relaxed)||available>=queue.target*2;let count=if running{available.min(out.len())/2*2}else{0};let start=read as usize%queue.samples.len();let first=count.min(queue.samples.len()-start);
    for(sample,value)in out[..first].iter_mut().zip(&queue.samples[start..start+first]){*sample=convert(f32::from_bits(value.load(Ordering::Relaxed)))}
    for(sample,value)in out[first..count].iter_mut().zip(&queue.samples[..count-first]){*sample=convert(f32::from_bits(value.load(Ordering::Relaxed)))}
    out[count..].fill(convert(0.0));
    if count>0 { queue.read.store(read+count as u64,Ordering::Release); queue.played.fetch_add((count/2)as u64,Ordering::Relaxed); }
    if running&&count<out.len() { queue.underruns.fetch_add(1,Ordering::Relaxed); queue.started.store(false,Ordering::Relaxed); }
    else if running { queue.started.store(true,Ordering::Relaxed); }
}

#[cfg(not(windows))]
fn create_stream(rate:u32,block:usize,target:usize,capacity:usize,selected:Option<&str>)->Result<(cpal::Stream,Arc<Queue>),String>{
    let device=select_device(selected)?;let supported=device.default_output_config().map_err(|e|e.to_string())?;if supported.channels()!=2||supported.sample_rate().0!=rate{return Err(format!("requested stereo {rate} Hz; endpoint is {} channels / {} Hz",supported.channels(),supported.sample_rate().0))}
    let format=supported.sample_format();let config=supported.into();let name=device.name().map_err(|e|e.to_string())?;let device_id=device_id(&name);let queue=Arc::new(Queue{samples:(0..capacity*2).map(|_|AtomicU32::new(0)).collect(),read:AtomicU64::new(0),write:AtomicU64::new(0),played:AtomicU64::new(0),underruns:AtomicU64::new(0),started:AtomicBool::new(false),failed:AtomicBool::new(false),closed:AtomicBool::new(false),heartbeat:AtomicU64::new(0),epoch:Instant::now(),target,block,rate,device:Mutex::new((name,device_id))});
    let callback_queue=queue.clone();let failed_queue=queue.clone();let on_error=move |_|failed_queue.failed.store(true,Ordering::Relaxed);
    let stream=match format{SampleFormat::F32=>device.build_output_stream(&config,move|o:&mut[f32],_|fill(o,&callback_queue,|v|v),on_error,None),SampleFormat::I16=>device.build_output_stream(&config,move|o:&mut[i16],_|fill(o,&callback_queue,|v|(v.clamp(-1.0,1.0)*i16::MAX as f32)as i16),on_error,None),SampleFormat::U16=>device.build_output_stream(&config,move|o:&mut[u16],_|fill(o,&callback_queue,|v|((v.clamp(-1.0,1.0)+1.0)*32767.5)as u16),on_error,None),_=>return Err("selected device sample format is unsupported".into())}.map_err(|e|e.to_string())?;stream.play().map_err(|e|e.to_string())?;Ok((stream,queue))
}

pub async fn open(Json(req):Json<OpenReq>)->Response{
    match tokio::task::spawn_blocking(move||open_blocking(req)).await { Ok(response)=>response,Err(join_error)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",join_error) }
}
fn open_blocking(req:OpenReq)->Response {
    let _control=CONTROL.lock().unwrap();
    if !(8000..=192000).contains(&req.sample_rate) || !(1..=8192).contains(&req.block_frames) || !(1..=128).contains(&req.target_blocks) {
        return error(StatusCode::BAD_REQUEST, "INVALID_AUDIO_CONFIG", "invalid sample rate or buffer settings");
    }
    let capacity=req.block_frames*(req.target_blocks+3);
    if capacity>1_048_576 { return error(StatusCode::BAD_REQUEST,"INVALID_AUDIO_CONFIG","queue too large"); }
    let target=req.block_frames*req.target_blocks;
    let mode=match parse_mode(req.mode.as_deref()){Ok(m)=>m,Err(e)=>return error(StatusCode::BAD_REQUEST,"INVALID_AUDIO_MODE",e)};
    #[cfg(not(windows))] if mode==super::audio_windows::Mode::Exclusive{return error(StatusCode::NOT_IMPLEMENTED,"EXCLUSIVE_MODE_UNAVAILABLE","exclusive mode is only available through Windows WASAPI")}
    let mut active=ACTIVE.lock().unwrap();
    if active.as_ref().is_some_and(|s|s.queue.closed.load(Ordering::Acquire)) { active.take(); }
    if active.is_some() { return error(StatusCode::CONFLICT,"OUTPUT_BUSY","a native output stream is already open"); }
    drop(active);
    #[cfg(windows)] {
        let endpoints=match super::audio_windows::endpoints(){Ok(d)=>d,Err(e)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e)};
        let endpoint=if let Some(id)=req.device_id.as_deref(){match endpoints.into_iter().find(|d|d.id==id){Some(d)=>d,None=>return error(StatusCode::BAD_REQUEST,"OUTPUT_DEVICE_NOT_FOUND","selected WASAPI endpoint unavailable")}}else{match endpoints.into_iter().find(|d|d.is_default){Some(d)=>d,None=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE","no default WASAPI endpoint")}};
        if !endpoint.selectable{return error(StatusCode::BAD_REQUEST,"OUTPUT_UNAVAILABLE",endpoint.reason.unwrap_or_else(||"endpoint not selectable".into()))}
        let queue=Arc::new(Queue{samples:(0..capacity*2).map(|_|AtomicU32::new(0)).collect(),read:AtomicU64::new(0),write:AtomicU64::new(0),played:AtomicU64::new(0),underruns:AtomicU64::new(0),started:AtomicBool::new(false),failed:AtomicBool::new(false),closed:AtomicBool::new(false),heartbeat:AtomicU64::new(0),epoch:Instant::now(),target,block:req.block_frames,rate:req.sample_rate,device:Mutex::new((endpoint.name.clone(),endpoint.id.clone()))});
        let(ready_tx,ready_rx)=mpsc::sync_channel(1);let(command_tx,command_rx)=mpsc::channel();let(done_tx,done_rx)=mpsc::channel();let(stop_tx,stop_rx)=mpsc::channel();let stop_command=command_tx.clone();let metadata=Arc::new(Mutex::new((endpoint.id.clone(),mode)));let thread_metadata=metadata.clone();let thread_queue=queue.clone();let selected=endpoint.id.clone();let rate=req.sample_rate;let startup_state=Arc::new(std::sync::atomic::AtomicU32::new(0));let thread_startup_state=startup_state.clone();
        let owner=std::thread::Builder::new().name("svs-wasapi-output".into()).spawn(move||{match super::audio_windows::prepare(Some(&selected),rate,mode,true){Err(e)=>{let _=ready_tx.try_send(Err(e));},Ok(stream)=>{if thread_startup_state.compare_exchange(0,1,Ordering::AcqRel,Ordering::Acquire).is_err(){drop(stream);}else{let _=ready_tx.send(Ok(()));let output_queue=thread_queue.clone();let watchdog=std::thread::spawn(move||loop{match stop_rx.recv_timeout(Duration::from_millis(100)){Ok(())|Err(mpsc::RecvTimeoutError::Disconnected)=>{let _=stop_command.send(super::audio_windows::Command::Stop);break},Err(mpsc::RecvTimeoutError::Timeout)=>{let idle=(output_queue.epoch.elapsed().as_millis()as u64).saturating_sub(output_queue.heartbeat.load(Ordering::Relaxed));if idle>5000||output_queue.failed.load(Ordering::Relaxed){let _=stop_command.send(super::audio_windows::Command::Stop);break}}}});super::audio_windows::run(stream,mode,rate,thread_queue.clone(),command_rx,thread_metadata);let _=watchdog.join();thread_queue.closed.store(true,Ordering::Release)}}}let _=done_tx.send(());});
        if let Err(e)=owner{return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e)}match ready_rx.recv_timeout(Duration::from_secs(8)){Ok(Ok(()))=>{},Ok(Err(e))=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e),Err(_)=>{match startup_state.compare_exchange(0,2,Ordering::AcqRel,Ordering::Acquire){Ok(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE","device initialization timed out; cancellation requested"),Err(1)=>match ready_rx.recv(){Ok(Ok(()))=>{},Ok(Err(e))=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e),Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE","device owner exited during startup")},Err(_)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE","device initialization timed out; cancellation requested")}}}
        let session=Arc::new(Session{id:uuid::Uuid::new_v4().to_string(),queue,sequence:Mutex::new(0),stop:stop_tx,done:Mutex::new(done_rx),switch:command_tx,metadata});let response=json!({"id":session.id,"sample_rate":rate,"channels":2,"device":endpoint.name,"device_id":endpoint.id,"mode":if mode==super::audio_windows::Mode::Shared{"shared"}else{"exclusive"},"capacity_frames":capacity,"target_frames":target});*ACTIVE.lock().unwrap()=Some(session);return Json(response).into_response()
    }
    #[cfg(not(windows))] {
        let(stream,queue)=match create_stream(req.sample_rate,req.block_frames,target,capacity,req.device_id.as_deref()){Ok(x)=>x,Err(e)=>return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e)};
        let(stop_tx,stop_rx)=mpsc::channel();let(done_tx,done_rx)=mpsc::channel();let owner_queue=queue.clone();
        let owner=std::thread::Builder::new().name("svs-device-output".into()).spawn(move||{let _stream=stream;loop{if stop_rx.recv_timeout(Duration::from_millis(100))!=Err(mpsc::RecvTimeoutError::Timeout){break}let idle=(owner_queue.epoch.elapsed().as_millis()as u64).saturating_sub(owner_queue.heartbeat.load(Ordering::Relaxed));if idle>5000||owner_queue.failed.load(Ordering::Relaxed){break}}owner_queue.closed.store(true,Ordering::Release);let _=done_tx.send(());});
        if let Err(e)=owner{return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_UNAVAILABLE",e)}
        let(tx,_rx)=closed_switch_channel();let initial_device=queue.device.lock().unwrap().clone();let metadata=Arc::new(Mutex::new((initial_device.1.clone(),Mode::Shared)));let session=Arc::new(Session{id:uuid::Uuid::new_v4().to_string(),queue:queue.clone(),sequence:Mutex::new(0),stop:stop_tx,done:Mutex::new(done_rx),switch:tx,metadata});let response=json!({"id":session.id,"sample_rate":queue.rate,"channels":2,"device":initial_device.0,"device_id":initial_device.1,"mode":"shared","capacity_frames":capacity,"target_frames":target});*ACTIVE.lock().unwrap()=Some(session);Json(response).into_response()
    }
}
#[cfg(not(windows))]
fn closed_switch_channel() -> (mpsc::Sender<super::audio_windows::Command>, mpsc::Receiver<super::audio_windows::Command>) { mpsc::channel() }
fn session(id:&str)->Option<Arc<Session>>{ACTIVE.lock().unwrap().as_ref().filter(|s|s.id==id).cloned()}
pub async fn switch_device(Path(id):Path<String>,Json(req):Json<SwitchReq>)->Response{
    match tokio::task::spawn_blocking(move||switch_device_blocking(id,req)).await { Ok(response)=>response,Err(join_error)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_SWITCH_FAILED",join_error) }
}
fn switch_device_blocking(id:String,req:SwitchReq)->Response{
    let _control=CONTROL.lock().unwrap();
    let Some(session)=session(&id)else{return error(StatusCode::NOT_FOUND,"OUTPUT_NOT_FOUND","unknown output stream")};
    if session.queue.closed.load(Ordering::Acquire)||session.queue.failed.load(Ordering::Acquire){return error(StatusCode::GONE,"OUTPUT_CLOSED","device stream closed or failed")}
    if req.sample_rate!=session.queue.rate{return error(StatusCode::CONFLICT,"SAMPLE_RATE_CHANGE_REQUIRES_STOP","stop playback before changing sample rate")}
    let mode=match parse_mode(Some(&req.mode)){Ok(m)=>m,Err(e)=>return error(StatusCode::BAD_REQUEST,"INVALID_AUDIO_MODE",e)};
    #[cfg(not(windows))]{let _=(mode,req);return error(StatusCode::NOT_IMPLEMENTED,"OUTPUT_SWITCH_UNAVAILABLE","live device switching requires native WASAPI")}
    #[cfg(windows)]{
        let(tx,rx)=mpsc::sync_channel(1);
        let deadline=Instant::now()+Duration::from_secs(8);
        if session.switch.send(super::audio_windows::Command::Switch{id:req.device_id,mode,deadline,response:tx}).is_err(){return error(StatusCode::GONE,"OUTPUT_CLOSED","output owner stopped")}
        match rx.recv(){
            Ok(Ok((dev,name,actual_mode)))=>Json(json!({"device_id":dev,"sample_rate":session.queue.rate,"device":name,"mode":if actual_mode==super::audio_windows::Mode::Shared{"shared"}else{"exclusive"}})).into_response(),
            Ok(Err(e))=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_SWITCH_FAILED",e),
            Err(_)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_CLOSED","output owner exited without a switch result"),
        }
    }
}
pub async fn pcm(Path(id):Path<String>,headers:HeaderMap,body:Bytes)->Response{
    let Some(session)=session(&id)else{return error(StatusCode::NOT_FOUND,"OUTPUT_NOT_FOUND","unknown output stream")};let q=&session.queue;if q.closed.load(Ordering::Acquire)||q.failed.load(Ordering::Relaxed){return error(StatusCode::GONE,"OUTPUT_CLOSED","device stream closed or failed")};let Some(sequence)=headers.get("x-audio-sequence").and_then(|h|h.to_str().ok()).and_then(|h|h.parse::<u64>().ok())else{return error(StatusCode::BAD_REQUEST,"INVALID_SEQUENCE","x-audio-sequence required")};if body.len()!=q.block*8{return error(StatusCode::BAD_REQUEST,"INVALID_PCM","exactly one stereo f32le block required")};if body.chunks_exact(4).any(|v|!f32::from_le_bytes(v.try_into().unwrap()).is_finite()){return error(StatusCode::BAD_REQUEST,"INVALID_PCM","samples must be finite")};let mut next=session.sequence.lock().unwrap();if sequence!=*next{return error(StatusCode::CONFLICT,"SEQUENCE_MISMATCH",format!("expected {}",*next))};let write=q.write.load(Ordering::Relaxed);let used=write-q.read.load(Ordering::Acquire);if q.samples.len()-(used as usize)<body.len()/4{return error(StatusCode::CONFLICT,"OUTPUT_FULL","device queue full")};let start=write as usize%q.samples.len();let count=body.len()/4;let first=count.min(q.samples.len()-start);for(slot,value)in q.samples[start..start+first].iter().chain(q.samples[..count-first].iter()).zip(body.chunks_exact(4)){slot.store(u32::from_le_bytes(value.try_into().unwrap()),Ordering::Relaxed)}q.write.store(write+count as u64,Ordering::Release);*next+=1;q.touch();Json(q.status(&id)).into_response()
}
pub async fn status(Path(id):Path<String>)->Response{let Some(session)=session(&id)else{return error(StatusCode::NOT_FOUND,"OUTPUT_NOT_FOUND","unknown output stream")};session.queue.touch();Json(session.queue.status(&id)).into_response()}
pub async fn close(Path(id):Path<String>)->Response{
    match tokio::task::spawn_blocking(move||close_blocking(id)).await { Ok(response)=>response,Err(join_error)=>error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_CLOSE_FAILED",join_error) }
}
fn close_blocking(id:String)->Response {
    let _control=CONTROL.lock().unwrap();
    let session={ACTIVE.lock().unwrap().as_ref().filter(|session|session.id==id).cloned()};
    if let Some(session)=session {
        let _=session.switch.send(super::audio_windows::Command::Stop);let _=session.stop.send(());
        if session.done.lock().unwrap().recv_timeout(Duration::from_secs(3)).is_err(){return error(StatusCode::SERVICE_UNAVAILABLE,"OUTPUT_CLOSE_FAILED","device close timed out")}
        let mut active=ACTIVE.lock().unwrap();if active.as_ref().is_some_and(|current|Arc::ptr_eq(current,&session)){active.take();}
    }
    StatusCode::NO_CONTENT.into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]fn status_never_observes_a_mixed_device_identity(){let q=Arc::new(queue(8,0));*q.device.lock().unwrap()=("0".into(),"0".into());let writer=Arc::clone(&q);let updates=std::thread::spawn(move||{for index in 0..10_000{let identity=index.to_string();*writer.device.lock().unwrap()=(identity.clone(),identity);}});for _ in 0..10_000{let status=q.status("session");assert_eq!(status["device"],status["device_id"]);}updates.join().unwrap();}
    fn queue(capacity:usize,target:usize)->Queue{Queue{samples:(0..capacity).map(|_|AtomicU32::new(0)).collect::<Vec<_>>().into_boxed_slice(),read:AtomicU64::new(0),write:AtomicU64::new(0),played:AtomicU64::new(0),underruns:AtomicU64::new(0),started:AtomicBool::new(false),failed:AtomicBool::new(false),closed:AtomicBool::new(false),heartbeat:AtomicU64::new(0),epoch:Instant::now(),target,block:2,rate:48000,device:Mutex::new(("test".into(),"test-id".into()))}}
    #[test]fn fill_waits_for_target_and_preserves_stereo(){let q=queue(8,2);q.samples[0].store(0.25f32.to_bits(),Ordering::Relaxed);q.samples[1].store((-0.25f32).to_bits(),Ordering::Relaxed);q.write.store(2,Ordering::Release);let mut out=[9.0f32;4];fill(&mut out,&q,|v|v);assert_eq!(out,[0.0;4]);assert_eq!(q.read.load(Ordering::Relaxed),0);q.samples[2].store(0.5f32.to_bits(),Ordering::Relaxed);q.samples[3].store((-0.5f32).to_bits(),Ordering::Relaxed);q.write.store(4,Ordering::Release);fill(&mut out,&q,|v|v);assert_eq!(out,[0.25,-0.25,0.5,-0.5]);assert_eq!(q.played.load(Ordering::Relaxed),2);}
    #[test]fn fill_handles_ring_wrap_and_underrun(){let q=queue(4,0);q.samples[2].store(0.75f32.to_bits(),Ordering::Relaxed);q.samples[3].store((-0.75f32).to_bits(),Ordering::Relaxed);q.samples[0].store(0.5f32.to_bits(),Ordering::Relaxed);q.samples[1].store((-0.5f32).to_bits(),Ordering::Relaxed);q.read.store(2,Ordering::Relaxed);q.write.store(6,Ordering::Release);let mut out=[0.0f32;6];fill(&mut out,&q,|v|v);assert_eq!(out,[0.75,-0.75,0.5,-0.5,0.0,0.0]);assert_eq!(q.read.load(Ordering::Relaxed),6);assert_eq!(q.underruns.load(Ordering::Relaxed),1);assert!(!q.started.load(Ordering::Relaxed));}
}
