//! Reject browser cross-origin mutation and DNS rebinding before handlers run.
use axum::{extract::Request, http::{header, HeaderMap, StatusCode}, middleware::Next, response::{IntoResponse, Response}};

fn one_header<'a>(headers: &'a HeaderMap, name: &str) -> Result<Option<&'a str>, ()> {
    let mut values = headers.get_all(name).iter();
    let value = values.next().map(|value| value.to_str().map_err(|_| ()) ).transpose()?;
    if values.next().is_some() { return Err(()); }
    Ok(value)
}

fn permitted(headers: &HeaderMap) -> bool {
    let Ok(Some(host)) = one_header(headers, header::HOST.as_str()) else { return false; };
    if !["127.0.0.1:8761", "localhost:8761"].contains(&host) { return false; }
    let Ok(origin) = one_header(headers, header::ORIGIN.as_str()) else { return false; };
    if let Some(origin) = origin {
        return ["http://tauri.localhost", "http://127.0.0.1:8761", "http://localhost:8761", "http://127.0.0.1:5173", "http://localhost:5173"].contains(&origin);
    }
    // Cross-site navigations/subresources can omit Origin. Native clients omit
    // Fetch Metadata as well and remain supported; these headers are not auth.
    matches!(one_header(headers, "sec-fetch-site"), Ok(None | Some("none" | "same-origin" | "same-site")))
}

pub async fn guard(request: Request, next: Next) -> Response {
    if !permitted(request.headers()) {
        return (StatusCode::FORBIDDEN, "Local API rejects this Host or browser origin").into_response();
    }
    next.run(request).await
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_cross_origin_and_rebinding_before_dispatch() {
        let mut headers = HeaderMap::new();
        headers.insert(header::HOST, "127.0.0.1:8761".parse().unwrap());
        assert!(permitted(&headers));
        headers.insert(header::ORIGIN, "https://attacker.invalid".parse().unwrap());
        assert!(!permitted(&headers));
        headers.insert(header::ORIGIN, "http://tauri.localhost".parse().unwrap());
        assert!(permitted(&headers));
        headers.insert(header::HOST, "attacker.invalid:8761".parse().unwrap());
        assert!(!permitted(&headers));
        headers.insert(header::HOST, "127.0.0.1:8761".parse().unwrap());
        headers.remove(header::ORIGIN);
        headers.insert("sec-fetch-site", "cross-site".parse().unwrap());
        assert!(!permitted(&headers));
        headers.remove("sec-fetch-site");
        headers.append(header::HOST, "localhost:8761".parse().unwrap());
        assert!(!permitted(&headers));
    }
}
