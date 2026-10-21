// Native WASAPI endpoint playback. COM operations stay on the owning output thread.
use std::{sync::Arc, time::{Duration, Instant}};

pub struct Endpoint {
    pub id: String,
    pub name: String,
    pub is_default: bool,
    pub sample_rate: Option<u32>,
    pub channels: Option<u16>,
    pub selectable: bool,
    pub reason: Option<String>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode { Shared, Exclusive }

#[cfg(windows)]
fn com_init() -> Result<(), String> {
    use windows::Win32::System::Com::*;
    unsafe {
        let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
        if hr.is_err() { Err(hr.to_string()) } else { Ok(()) }
    }
}

#[cfg(windows)]
pub fn endpoints() -> Result<Vec<Endpoint>, String> {
    use windows::{Win32::{Devices::FunctionDiscovery::PKEY_Device_FriendlyName, Media::Audio::*, System::Com::*}};
    unsafe {
        com_init()?;
        let result = (|| {
            let enumerator: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|e| e.to_string())?;
            let default_id = enumerator.GetDefaultAudioEndpoint(eRender, eMultimedia).ok().and_then(|d| d.GetId().ok()).map(|p| {
                let value = p.to_string().unwrap_or_default(); CoTaskMemFree(Some(p.0 as _)); value
            });
            let devices = enumerator.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE).map_err(|e| e.to_string())?;
            let mut endpoints = Vec::new();
            for index in 0..devices.GetCount().map_err(|e| e.to_string())? {
                let device = devices.Item(index).map_err(|e| e.to_string())?;
                let wide_id = device.GetId().map_err(|e| e.to_string())?;
                let id = wide_id.to_string().map_err(|e| e.to_string())?;
                CoTaskMemFree(Some(wide_id.0 as _));
                let name = device.OpenPropertyStore(STGM_READ).ok()
                    .and_then(|store| store.GetValue(&PKEY_Device_FriendlyName).ok())
                    .map(|value| value.to_string()).unwrap_or_else(|| id.clone());
                let (mut sample_rate, mut channels) = (None, None);
                if let Ok(client) = device.Activate::<IAudioClient>(CLSCTX_ALL, None) {
                    if let Ok(format) = client.GetMixFormat() {
                        if !format.is_null() {
                            sample_rate = Some((*format).nSamplesPerSec); channels = Some((*format).nChannels);
                            CoTaskMemFree(Some(format as _));
                        }
                    }
                }
                let selectable = sample_rate.is_some() && channels == Some(2);
                endpoints.push(Endpoint { id: id.clone(), name, is_default: default_id.as_deref() == Some(&id), sample_rate, channels, selectable,
                    reason: if selectable { None } else { Some("需要可读取的双声道设备格式".into()) } });
            }
            Ok(endpoints)
        })();
        CoUninitialize(); result
    }
}
#[cfg(not(windows))]
pub fn endpoints() -> Result<Vec<Endpoint>, String> { Ok(Vec::new()) }

pub fn asio_status() -> &'static str {
    "ASIO requires authorized SDK/license and a user-installed registered vendor driver; none are provisioned by this application"
}

#[cfg(windows)]
struct ComApartment;
#[cfg(windows)]
impl Drop for ComApartment { fn drop(&mut self) { unsafe { windows::Win32::System::Com::CoUninitialize(); } } }

#[cfg(windows)]
pub struct Stream {
    client: windows::Win32::Media::Audio::IAudioClient,
    render: windows::Win32::Media::Audio::IAudioRenderClient,
    event: windows::Win32::Foundation::HANDLE,
    frames: u32,
    bits: u16,
    pub sample_rate: u32,
    _com: ComApartment,
}
#[cfg(windows)]
impl Drop for Stream {
    fn drop(&mut self) { unsafe { let _ = self.client.Stop(); windows::Win32::Foundation::CloseHandle(self.event); } }
}

#[cfg(windows)]
pub fn prepare(id: Option<&str>, rate: u32, mode: Mode, start: bool) -> Result<Stream, String> {
    use windows::{core::*, Win32::{Media::Audio::*, System::{Com::*, Threading::*}}};
    unsafe {
        com_init()?;
        let result = (|| {
            let enumerator: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(|e| e.to_string())?;
            let device = if let Some(id) = id {
                let wide: Vec<u16> = id.encode_utf16().chain(Some(0)).collect();
                enumerator.GetDevice(PCWSTR(wide.as_ptr())).map_err(|e| e.to_string())?
            } else { enumerator.GetDefaultAudioEndpoint(eRender, eMultimedia).map_err(|e| e.to_string())? };
            let client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(|e| e.to_string())?;
            let share = if mode == Mode::Shared { AUDCLNT_SHAREMODE_SHARED } else { AUDCLNT_SHAREMODE_EXCLUSIVE };
            let candidates = if mode == Mode::Shared { [32u16, 0] } else { [32u16, 16] };
            let mut selected = None;
            for bits in candidates {
                if bits == 0 { continue; }
                let tag = if bits == 32 { 3 } else { 1 }; let bytes = (bits / 8) as u16;
                let format = WAVEFORMATEX { wFormatTag:tag, nChannels:2, nSamplesPerSec:rate, nAvgBytesPerSec:rate * bytes as u32 * 2, nBlockAlign:bytes * 2, wBitsPerSample:bits, cbSize:0 };
                // Shared-mode probing requires a non-null closest-format output.
                let mut closest = std::ptr::null_mut();
                let supported = client.IsFormatSupported(share, &format, if mode == Mode::Shared { Some(&mut closest) } else { None });
                if !closest.is_null() { CoTaskMemFree(Some(closest as _)); }
                if supported.0 == 0 { selected = Some((format, bits)); break; }
            }
            let (format, bits) = selected.ok_or_else(|| format!("exact stereo format unavailable at {rate} Hz in {mode:?} mode"))?;
            let mut device_period = 0i64;
            let _ = client.GetDevicePeriod(Some(&mut device_period), None);
            let period = device_period.max(100_000);
            client.Initialize(share, AUDCLNT_STREAMFLAGS_EVENTCALLBACK, period, if mode == Mode::Shared { 0 } else { period }, &format, None).map_err(|e| e.to_string())?;
            let event = CreateEventW(None, false, false, None).map_err(|e| e.to_string())?;
            if let Err(e) = client.SetEventHandle(event) { windows::Win32::Foundation::CloseHandle(event); return Err(e.to_string()); }
            let frames = client.GetBufferSize().map_err(|e| { windows::Win32::Foundation::CloseHandle(event); e.to_string() })?;
            let render: IAudioRenderClient = client.GetService().map_err(|e| { windows::Win32::Foundation::CloseHandle(event); e.to_string() })?;
            if start { if let Err(e) = client.Start() { windows::Win32::Foundation::CloseHandle(event); return Err(e.to_string()); } }
            Ok(Stream { client, render, event, frames, bits, sample_rate:rate, _com:ComApartment })
        })();
        match result { Ok(stream) => Ok(stream), Err(e) => { CoUninitialize(); Err(e) } }
    }
}

pub enum Command {
    Switch { id:String, mode:Mode, deadline:Instant, response:std::sync::mpsc::SyncSender<Result<(String,String,Mode),String>> },
    Stop,
}
#[cfg(windows)]
pub fn run(mut stream:Stream, mut mode:Mode, rate:u32, queue:Arc<super::audio_output::Queue>, commands:std::sync::mpsc::Receiver<Command>, metadata:Arc<std::sync::Mutex<(String,Mode)>>) {
    use windows::Win32::System::Threading::*;
    let mut failed=false;
    let mut pending:Option<(Instant,std::sync::mpsc::SyncSender<Result<(String,String,Mode),String>>,String,String,Mode)>=None;
    let mut last_render=Instant::now();
    loop {
        match commands.try_recv() {
            Ok(Command::Stop) => break,
            Ok(Command::Switch { id:next_id, mode:next_mode, deadline, response }) => {
                if pending.is_some() {
                    let _=response.send(Err("another device switch is awaiting render acknowledgement".into()));
                    continue;
                }
                if queue.closed.load(std::sync::atomic::Ordering::Acquire) || queue.failed.load(std::sync::atomic::Ordering::Acquire) {
                    let _=response.send(Err("output stream is closed or failed".into()));
                    continue;
                }
                if Instant::now() >= deadline {
                    let _=response.send(Err("device switch request expired".into()));
                    continue;
                }
                let endpoint=match endpoints() {
                    Ok(items)=>items.into_iter().find(|item|item.id==next_id),
                    Err(e)=>{let _=response.send(Err(e));continue;}
                };
                let Some(endpoint)=endpoint else {
                    let _=response.send(Err("selected WASAPI endpoint is unavailable".into()));
                    continue;
                };
                if !endpoint.selectable {
                    let _=response.send(Err(endpoint.reason.unwrap_or_else(||"endpoint not selectable".into())));
                    continue;
                }
                let display_name=endpoint.name;
                match prepare(Some(&next_id), rate, next_mode, false) {
                    Err(e) => { let _=response.send(Err(e)); }
                    Ok(next) if Instant::now() >= deadline => { drop(next); let _=response.send(Err("device switch request expired".into())); }
                    Ok(next) => {
                        if let Err(e)=unsafe { stream.client.Stop() } {
                            let restored=unsafe { stream.client.Start() }.is_ok();
                            drop(next);
                            let _=response.send(Err(e.to_string()));
                            if !restored { failed=true; break; }
                            continue;
                        }
                        match unsafe { next.client.Start() } {
                            Ok(()) => {
                                drop(stream);
                                stream=next;
                                mode=next_mode;
                                last_render=Instant::now();
                                pending=Some((deadline,response,next_id,display_name,next_mode));
                            }
                            Err(e) => {
                                let restored=unsafe { stream.client.Start() }.is_ok();
                                drop(next);
                                let _=response.send(Err(e.to_string()));
                                if !restored { failed=true; break; }
                            }
                        }
                    }
                }
            }
            Err(std::sync::mpsc::TryRecvError::Disconnected) => break,
            Err(std::sync::mpsc::TryRecvError::Empty) => {}
        }
        let wait=unsafe { WaitForSingleObject(stream.event,20) };
        if wait==windows::Win32::Foundation::WAIT_TIMEOUT { if last_render.elapsed()>Duration::from_secs(2) { failed=true; break; } continue; }
        if wait!=windows::Win32::Foundation::WAIT_OBJECT_0 { failed=true; break; }
        let render_result=unsafe {
            let count=if mode==Mode::Exclusive { stream.frames } else { match stream.client.GetCurrentPadding() { Ok(p)=>stream.frames.saturating_sub(p),Err(_)=>{failed=true;break;} } };
            if count==0 { continue; }
            let ptr=match stream.render.GetBuffer(count) { Ok(p)=>p,Err(_)=>{failed=true;break;} };
            if stream.bits==32 { let out=std::slice::from_raw_parts_mut(ptr as *mut f32,count as usize*2); super::audio_output::fill(out,&queue,|v|v); }
            else { let out=std::slice::from_raw_parts_mut(ptr as *mut i16,count as usize*2); super::audio_output::fill(out,&queue,|v|(v.clamp(-1.0,1.0)*i16::MAX as f32) as i16); }
            stream.render.ReleaseBuffer(count,0)
        };
        if render_result.is_err() { failed=true; let _=unsafe { stream.client.Stop() }; break; }
        last_render=Instant::now();
        if let Some((deadline,tx,committed_id,display_name,committed_mode))=pending.take() {
            if Instant::now()>=deadline {
                failed=true;
                let _=tx.send(Err("device switch exceeded its deadline; output stopped".into()));
                break;
            }
            *queue.device.lock().unwrap()=(display_name.clone(),committed_id.clone());
            *metadata.lock().unwrap()=(committed_id.clone(),committed_mode);
            let _=tx.send(Ok((committed_id,display_name,committed_mode)));
        }
    }
    if let Some((_,tx,_,_,_))=pending.take() { let _=tx.send(Err("output stopped before device switch was acknowledged".into())); }
    drop(stream);
    if failed { queue.failed.store(true,std::sync::atomic::Ordering::Relaxed); }
    queue.closed.store(true,std::sync::atomic::Ordering::Release);
}
