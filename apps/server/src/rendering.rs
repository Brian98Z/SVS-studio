//! One offline mixer path for synthesis and imported stereo clips.
use crate::{engine,state::AppState};
use svs_core::{project::{Project,TrackKind},timeline::tick_to_samples,SAMPLE_RATE};
use serde_json::{json,Value};
use base64::Engine;
use base64::engine::general_purpose::STANDARD as B64;

fn encode(pcm:&[f32])->String {
    let mut bytes=Vec::with_capacity(pcm.len()*4);
    for v in pcm {bytes.extend_from_slice(&v.to_le_bytes());}
    B64.encode(bytes)
}
fn decode(value:&Value)->anyhow::Result<Vec<f32>> {
    let bytes=B64.decode(value.get("audio").or_else(||value.get("audio_base64")).and_then(|v|v.as_str()).ok_or_else(||anyhow::anyhow!("missing audio"))?)?;
    anyhow::ensure!(bytes.len()%4==0,"unaligned PCM");
    let pcm:Vec<f32>=bytes.chunks_exact(4).map(|c|f32::from_le_bytes([c[0],c[1],c[2],c[3]])).collect();
    anyhow::ensure!(pcm.iter().all(|v|v.is_finite()),"nonfinite PCM");
    Ok(pcm)
}
pub async fn mix_project(st:&AppState,project:&Project)->anyhow::Result<Vec<f32>> {
    let base=st.inference_url.read().await.clone().ok_or_else(||anyhow::anyhow!("INFERENCE_DOWN"))?;
    let total=tick_to_samples(&project.tempos,project.end_tick()+svs_core::PPQ) as usize+1;
    let mut tracks=Vec::new();
    for track in &project.tracks {
        if !project.track_audible(track) || track.kind==TrackKind::Chord {continue;}
        let channels=if track.kind==TrackKind::Audio{2}else{1};
        let mut pcm=vec![0.;total*channels];
        if track.kind==TrackKind::Audio {
            for clip in project.audio_clips.iter().filter(|c|c.track_id==track.id) {
                let response=st.http.post(format!("{base}/media/decode_audio")).json(&json!({"path":clip.path,"sample_rate":SAMPLE_RATE})).send().await?;
                anyhow::ensure!(response.status().is_success(),"media decode: {}",response.text().await?);
                let chunk=decode(&response.json::<Value>().await?)?;
                let start=tick_to_samples(&project.tempos,clip.start_tick) as usize;
                let frames=chunk.len()/2;
                if pcm.len()<(start+frames)*2 {pcm.resize((start+frames)*2,0.);}
                let fade_in=clip.fade_in_ms.max(0.)*SAMPLE_RATE as f64/1000.;
                let fade_out=clip.fade_out_ms.max(0.)*SAMPLE_RATE as f64/1000.;
                for frame in 0..frames {
                    let gain=clip.gain*(if fade_in>0.{(frame as f64/fade_in).min(1.) as f32}else{1.})
                        *(if fade_out>0.{((frames-1-frame) as f64/fade_out).min(1.) as f32}else{1.});
                    for ch in 0..2 {pcm[(start+frame)*2+ch]+=chunk[frame*2+ch]*gain;}
                }
            }
        }else{
            let native=track.kind==TrackKind::Sing&&(track.singer_id.starts_with("diffsinger")||track.singer_id.starts_with("voice."));
            let timbre=if track.kind==TrackKind::Instrument {Some(if track.instrument.contains("drum"){ "drum" }else if track.instrument.contains("bass")||track.name.contains("贝斯"){ "bass" }else{"piano"})}else{None};
            for notes in engine::phrase_groups(track) {
                let unit=engine::phrase_spec(track,&notes,&project.tempos,native,timbre);
                let chunk=engine::fetch_audio(st,&unit.payload,unit.endpoint,if native{1800}else{120}).await?;
                let start=tick_to_samples(&project.tempos,unit.offset_tick) as usize;
                if pcm.len()<start+chunk.len(){pcm.resize(start+chunk.len(),0.);}
                for(i,v)in chunk.iter().enumerate(){pcm[start+i]+=v;}
            }
        }
        tracks.push(json!({"id":track.id,"audio":encode(&pcm),"channels":channels,"volume":track.volume,"pan":track.pan,
            "bus_gain":if track.kind==TrackKind::Sing{1.45}else if track.kind==TrackKind::Instrument{0.8}else{1.},
            "fx":track.fx,"sends":track.sends,"bus":if track.bus.is_empty(){"master"}else{&track.bus}}));
    }
    let response=st.http.post(format!("{base}/mix")).json(&json!({"tracks":tracks,"sample_rate":SAMPLE_RATE,
        "master_volume":project.master_volume,"master_fx":project.mixer.pointer("/master/effects").cloned().unwrap_or(json!([])),
        "buses":project.mixer.get("buses").cloned().unwrap_or(json!([]))})).timeout(std::time::Duration::from_secs(300)).send().await?;
    anyhow::ensure!(response.status().is_success(),"mix: {}",response.text().await?);
    decode(&response.json::<Value>().await?)
}
