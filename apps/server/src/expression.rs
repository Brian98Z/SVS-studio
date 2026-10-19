use crate::state::AppState;
use axum::extract::{Path, State};
use axum::response::IntoResponse;
use axum::Json;
use serde::Deserialize;
use serde_json::json;
use svs_core::phrase::{Note, Vibrato};

pub(crate) fn nullable_patch<'de, D, T>(deserializer: D) -> Result<Option<Option<T>>, D::Error>
where D: serde::Deserializer<'de>, T: serde::Deserialize<'de> {
    Option::<T>::deserialize(deserializer).map(Some)
}
#[derive(Deserialize)]
pub struct ExpressionRequest {
    pub note_ids: Vec<String>,
    #[serde(default, deserialize_with = "nullable_patch")]
    pub vibrato: Option<Option<Vibrato>>,
    #[serde(default, deserialize_with = "nullable_patch")]
    pub attack_ms: Option<Option<f32>>,
    #[serde(default, deserialize_with = "nullable_patch")]
    pub decay_ms: Option<Option<f32>>,
    pub kind: Option<String>,
}

fn valid(v: &ExpressionRequest) -> bool {
    v.note_ids.len() > 0
        && v.note_ids.iter().collect::<std::collections::HashSet<_>>().len() == v.note_ids.len()
        && v.kind.as_ref().map_or(true, |k| k == "sung" || k == "breath")
        && v.attack_ms.flatten().map_or(true, |x| x.is_finite() && x >= 0.0)
        && v.decay_ms.flatten().map_or(true, |x| x.is_finite() && x >= 0.0)
        && v.vibrato.as_ref().and_then(Option::as_ref).map_or(true, |x| {
            x.depth_cents.is_finite() && x.depth_cents >= 0.0
                && x.frequency_hz.is_finite() && x.frequency_hz > 0.0
                && x.delay_ms.is_finite() && x.delay_ms >= 0.0
                && x.onset_ms.is_finite() && x.onset_ms >= 0.0
                && x.release_ms.is_finite() && x.release_ms >= 0.0
        })
}

fn apply(note: &mut Note, request: &ExpressionRequest) {
    if let Some(value) = &request.kind { note.kind.clone_from(value); }
    if let Some(value) = &request.vibrato { note.vibrato.clone_from(value); }
    if let Some(value) = request.attack_ms { note.attack_ms = value; }
    if let Some(value) = request.decay_ms { note.decay_ms = value; }
}

pub async fn update_track_expression(
    State(state): State<AppState>, Path(track_id): Path<String>, Json(request): Json<ExpressionRequest>,
) -> axum::response::Response {
    if !valid(&request) {
        return (axum::http::StatusCode::BAD_REQUEST, Json(json!({"error":"VALIDATION","detail":"invalid expression request"}))).into_response();
    }
    state.snapshot().await;
    let mut project = state.project.write().await;
    let Some(track) = project.track_mut(&track_id) else {
        return (axum::http::StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND","detail":"track"}))).into_response();
    };
    if request.note_ids.iter().any(|id| !track.notes.iter().any(|note| note.id == *id)) {
        return (axum::http::StatusCode::NOT_FOUND, Json(json!({"error":"NOT_FOUND","detail":"note"}))).into_response();
    }
    for note in &mut track.notes {
        if request.note_ids.iter().any(|id| id == &note.id) { apply(note, &request); }
    }
    state.bump_rev();
    drop(project);
    (axum::http::StatusCode::OK, Json(json!({"ok":true,"updated":request.note_ids.len()}))).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_null_clears_while_missing_preserves_expression() {
        let clear: ExpressionRequest = serde_json::from_value(json!({"note_ids":["n"],"vibrato":null,"attack_ms":null})).unwrap();
        assert!(matches!(clear.vibrato, Some(None)));
        assert_eq!(clear.attack_ms, Some(None));
        let absent: ExpressionRequest = serde_json::from_value(json!({"note_ids":["n"]})).unwrap();
        assert!(absent.vibrato.is_none());
        assert!(absent.attack_ms.is_none());
    }
}
