use crate::{engine, state::AppState};
use axum::{extract::State, response::{IntoResponse,Response}, Json, http::StatusCode};
use serde::Deserialize;
use serde_json::json;

#[derive(Deserialize)]
pub struct MixerPatch { mixer: serde_json::Value }

pub async fn patch_mixer(State(st):State<AppState>,Json(req):Json<MixerPatch>)->Response {
    if let Err(e)=engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE,Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base)=st.inference_url.read().await.clone() else{return StatusCode::SERVICE_UNAVAILABLE.into_response()};
    let response=st.http.post(format!("{base}/mix/validate")).json(&req.mixer).send().await;
    let normalized=match response {
        Ok(r)=>{
            let status=r.status();
            let data=r.json::<serde_json::Value>().await.unwrap_or(json!({"error":"INVALID_MIXER_RESPONSE"}));
            if !status.is_success(){return (status,Json(data)).into_response()}
            data.get("mixer").cloned().unwrap_or(req.mixer)
        }
        Err(e)=>return (StatusCode::BAD_GATEWAY,Json(json!({"error":"MIXER_VALIDATION","detail":e.to_string()}))).into_response(),
    };
    st.snapshot().await;
    let mut p=st.project.write().await;
    if let Some(volume)=normalized.pointer("/master/volume").and_then(|v|v.as_f64()) {p.master_volume=volume.clamp(0.,1.) as f32;}
    p.mixer=normalized;
    st.bump_mix_rev();
    Json(json!({"ok":true,"mixer":p.mixer,"master_volume":p.master_volume})).into_response()
}
#[derive(Deserialize)]
pub struct EffectRead { kind: String, id: String, index: usize }

pub async fn read_effect(State(st):State<AppState>, Json(req):Json<EffectRead>)->Response {
    let p=st.project.read().await;
    let revision=st.edit_rev.load(std::sync::atomic::Ordering::SeqCst);
    match effect_at(&p.mixer, &p.tracks, &req.kind, &req.id, req.index) {
        Ok(effect)=>Json(json!({"effect":effect,"revision":revision})).into_response(),
        Err(status)=>status.into_response(),
    }
}

#[derive(Deserialize)]
pub struct EffectPatch { kind: String, id: String, index: usize, expected_revision: u64, parameters: serde_json::Value, #[serde(default)] state_base64: Option<String> }

pub async fn patch_effect(State(st):State<AppState>, Json(req):Json<EffectPatch>)->Response {
    use std::sync::atomic::Ordering;
    let candidate={
        let p=st.project.read().await;
        if req.expected_revision!=st.edit_rev.load(Ordering::SeqCst) { return stale_project(); }
        let mut effect=match effect_at(&p.mixer, &p.tracks, &req.kind, &req.id, req.index) {
            Ok(effect)=>effect.clone(), Err(status)=>return status.into_response(),
        };
        effect["parameters"]=req.parameters.clone();
        if let Some(state)=&req.state_base64 { effect["state_base64"]=json!(state); }
        json!({"master":{"effects":[effect]},"buses":[]})
    };
    if let Err(e)=engine::ensure_audio_service(&st).await {
        return (StatusCode::SERVICE_UNAVAILABLE,Json(json!({"error":"INFERENCE_DOWN","detail":e.to_string()}))).into_response();
    }
    let Some(base)=st.inference_url.read().await.clone() else{return StatusCode::SERVICE_UNAVAILABLE.into_response()};
    let response=st.http.post(format!("{base}/mix/validate")).json(&candidate).send().await;
    match response {
        Ok(r)=>{let status=r.status();if !status.is_success(){let data=r.json::<serde_json::Value>().await.unwrap_or(json!({"error":"INVALID_MIXER_RESPONSE"}));return (status,Json(data)).into_response()}},
        Err(e)=>return (StatusCode::BAD_GATEWAY,Json(json!({"error":"MIXER_VALIDATION","detail":e.to_string()}))).into_response(),
    }
    let mut p=st.project.write().await;
    if req.expected_revision!=st.edit_rev.load(Ordering::SeqCst) { return stale_project(); }
    st.history.lock().await.push(&*p);
    replace_parameters(&mut p, &req.kind, &req.id, req.index, req.parameters, req.state_base64).expect("target checked at unchanged revision");
    st.bump_mix_rev();
    Json(json!({"ok":true})).into_response()
}

fn stale_project()->Response { (StatusCode::CONFLICT,Json(json!({"error":"STALE_PROJECT"}))).into_response() }

fn effect_at<'a>(mixer:&'a serde_json::Value, tracks:&'a [svs_core::project::Track], kind:&str,id:&str,index:usize)->Result<&'a serde_json::Value,StatusCode> {
    let effects=match kind {"track"=>tracks.iter().find(|t|t.id==id).map(|t|&t.fx),"bus"=>mixer.get("buses").and_then(|v|v.as_array()).and_then(|b|b.iter().find(|v|v.get("id").and_then(|x|x.as_str())==Some(id))).and_then(|v|v.get("effects")).and_then(|v|v.as_array()),"master" if id=="master"=>mixer.pointer("/master/effects").and_then(|v|v.as_array()),_=>return Err(StatusCode::NOT_FOUND)}.ok_or(StatusCode::NOT_FOUND)?;
    let effect=effects.get(index).ok_or(StatusCode::NOT_FOUND)?;
    if effect.get("type").and_then(|v|v.as_str())!=Some("vst3") { return Err(StatusCode::BAD_REQUEST); }
    Ok(effect)
}

fn replace_parameters(project:&mut svs_core::project::Project,kind:&str,id:&str,index:usize,parameters:serde_json::Value,state_base64:Option<String>)->Result<(),StatusCode> {
    effect_at(&project.mixer,&project.tracks,kind,id,index)?;
    let effects=match kind {
        "track"=>project.tracks.iter_mut().find(|t|t.id==id).map(|t|&mut t.fx),
        "bus"=>project.mixer.get_mut("buses").and_then(|v|v.as_array_mut()).and_then(|b|b.iter_mut().find(|v|v.get("id").and_then(|x|x.as_str())==Some(id))).and_then(|v|v.get_mut("effects")).and_then(|v|v.as_array_mut()),
        "master"=>project.mixer.pointer_mut("/master/effects").and_then(|v|v.as_array_mut()),
        _=>None,
    }.ok_or(StatusCode::NOT_FOUND)?;
    effects[index]["parameters"]=parameters;
    if let Some(state)=state_base64 { effects[index]["state_base64"]=json!(state); }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use svs_core::project::{Project, Track, TrackKind};
    use std::sync::{Arc, atomic::Ordering};

    fn fixture() -> Project {
        let mut p=Project::new("editor");
        let effect=json!({"type":"vst3","path":"plugins/Nova.vst3","enabled":false,"block_frames":1024,"tail_seconds":2,"parameters":{"gain":0}});
        let mut track=Track::new(TrackKind::Audio,"track");
        track.id="track".into();track.fx=vec![effect.clone(),json!({"type":"eq3","enabled":true})];p.tracks.push(track);
        p.mixer=json!({"master":{"volume":0.5,"effects":[effect.clone()]},"buses":[{"id":"bus","effects":[effect]}]});
        p
    }

    #[test]
    fn replacing_parameters_preserves_other_effect_fields_and_channels() {
        for (kind,id) in [("track","track"),("bus","bus"),("master","master")] {
            let mut p=fixture();
            let before=p.to_json();
            replace_parameters(&mut p,kind,id,0,json!({"gain":-12}),None).unwrap();
            let effect=effect_at(&p.mixer,&p.tracks,kind,id,0).unwrap();
            assert_eq!(effect["parameters"],json!({"gain":-12}));
            replace_parameters(&mut p,kind,id,0,json!({"gain":0}),None).unwrap();
            assert_eq!(p.to_json(),before);
        }
        let mut p=fixture();let before=p.to_json();
        assert_eq!(replace_parameters(&mut p,"track","track",1,json!({}),None),Err(StatusCode::BAD_REQUEST));
        assert_eq!(replace_parameters(&mut p,"track","missing",0,json!({}),None),Err(StatusCode::NOT_FOUND));
        assert_eq!(replace_parameters(&mut p,"master","master",1,json!({}),None),Err(StatusCode::NOT_FOUND));
        assert_eq!(p.to_json(),before);
    }

    #[test]
    fn saved_state_survives_parameter_only_edits_and_project_serialization() {
        for (kind,id) in [("track","track"),("bus","bus"),("master","master")] {
            let mut p=fixture();
            replace_parameters(&mut p,kind,id,0,json!({}),Some("preset-bytes".into())).unwrap();
            replace_parameters(&mut p,kind,id,0,json!({"gain":-6}),None).unwrap();
            let reopened=Project::from_json(&p.to_json()).unwrap();
            let effect=effect_at(&reopened.mixer,&reopened.tracks,kind,id,0).unwrap();
            assert_eq!(effect["state_base64"],"preset-bytes");
            assert_eq!(effect["parameters"],json!({"gain":-6}));
            assert_eq!(effect["path"],"plugins/Nova.vst3");
            assert_eq!(effect["enabled"],false);
        }
    }

    #[tokio::test]
    async fn intervening_edit_during_validation_rejects_without_overwrite() {
        let st=AppState::new();*st.project.write().await=fixture();
        let entered=Arc::new(tokio::sync::Notify::new());
        let release=Arc::new(tokio::sync::Notify::new());
        let (seen,gate)=(entered.clone(),release.clone());
        let app=axum::Router::new().route("/mix/validate",axum::routing::post(move || async move {
            seen.notify_one();gate.notified().await;Json(json!({"ok":true}))
        }));
        let app=app.route("/health",axum::routing::get(|| async {StatusCode::OK}));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        *st.inference_url.write().await=Some(format!("http://{}",listener.local_addr().unwrap()));
        let server=tokio::spawn(async move {axum::serve(listener,app).await.unwrap()});
        let state=st.clone();
        let commit=tokio::spawn(async move {patch_effect(State(state),Json(EffectPatch {kind:"track".into(),id:"track".into(),index:0,expected_revision:0,parameters:json!({"gain":-12}),state_base64:None})).await});
        tokio::time::timeout(std::time::Duration::from_secs(5),entered.notified()).await.unwrap();
        {let mut p=st.project.write().await;p.tracks[0].name="newer edit".into();st.bump_mix_rev();}
        release.notify_one();
        assert_eq!(commit.await.unwrap().status(),StatusCode::CONFLICT);
        let p=st.project.read().await;
        assert_eq!(p.tracks[0].name,"newer edit");assert_eq!(p.tracks[0].fx[0]["parameters"],json!({"gain":0}));
        assert_eq!(st.edit_rev.load(Ordering::SeqCst),1);drop(p);
        let rejected=patch_effect(State(st.clone()),Json(EffectPatch {kind:"master".into(),id:"master".into(),index:0,expected_revision:0,parameters:json!({"gain":-6}),state_base64:None})).await;
        assert_eq!(rejected.status(),StatusCode::CONFLICT);
        server.abort();
    }
}
