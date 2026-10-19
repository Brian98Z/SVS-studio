//! svs-cli：REST 语义命令行（契约 §7）。
//!
//! 约定：所有命令打到 http://127.0.0.1:8761/api/*，与桌面端共享同一 server。
//! 特殊子命令 `conformance <op> ...`：契约对拍（无 server，纯 core 计算）。


fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        print_help();
        std::process::exit(2);
    }
    match run(&args) {
        Ok(out) => {
            println!("{out}");
            std::process::exit(0);
        }
        Err(e) => {
            eprintln!("error: {e}");
            std::process::exit(1);
        }
    }
}

fn print_help() {
    println!(r#"svs-cli — SVS Studio 命令行（契约 §7）

  new | open <path> | save [path]
  track add <kind> [name] | track list | track remove <id> | track set <id> --vol 1.0 --mute
  note add <track> --tick 0 --dur 480 --key 60 [--lyric 你]
  note list <track> | note del <track> <noteId>
  curve paint <track> --param pitch --layer user|envelope|direct|global --from 0 --to 1920 --value 200
  curve clear <track> --param pitch --layer user|envelope|direct|global --from 0 --to 1920
  track-speaker <track> <speaker-id> | track-blend <track> <blend.json>
  tempo set <tempos.json> | expression <track> <expression.json> | mixer set <mixer.json>
  interchange <import|export> <midi|musicxml|ufdata|lrc> <path>
  stems export <directory> | media import <path> [--track ID] [--tick N]
  media export <fcpxml|aaf> <output-path> | media video <video> <audio> <output>
  music-ai <vocal-to-midi|stems|score|accompaniment> <input-path> [--model MODEL] [--style STYLE]
  synth [track] | transport play|stop | render <out.wav> | health
  conformance tick-to-ms <temposJson> <tick>    # 契约对拍（离线）
"#);
}

fn required<'a>(it: &mut impl Iterator<Item = &'a String>, usage: &str) -> anyhow::Result<&'a String> {
    it.next().ok_or_else(|| anyhow::anyhow!("{usage}"))
}

fn json_file(path: &str) -> anyhow::Result<serde_json::Value> {
    let text = std::fs::read_to_string(path)?;
    Ok(serde_json::from_str(&text)?)
}

fn base64_encode(input: &[u8]) -> String {
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut output = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let a = chunk[0];
        let b = *chunk.get(1).unwrap_or(&0);
        let c = *chunk.get(2).unwrap_or(&0);
        output.push(TABLE[(a >> 2) as usize] as char);
        output.push(TABLE[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        output.push(if chunk.len() > 1 { TABLE[(((b & 15) << 2) | (c >> 6)) as usize] as char } else { '=' });
        output.push(if chunk.len() > 2 { TABLE[(c & 63) as usize] as char } else { '=' });
    }
    output
}
fn decode_hex(input: &str) -> anyhow::Result<Vec<u8>> {
    anyhow::ensure!(input.len() % 2 == 0, "hex content has odd length");
    input.as_bytes().chunks_exact(2).map(|pair| {
        let digit = |byte: u8| -> anyhow::Result<u8> {
            match byte {
                b'0'..=b'9' => Ok(byte - b'0'),
                b'a'..=b'f' => Ok(byte - b'a' + 10),
                b'A'..=b'F' => Ok(byte - b'A' + 10),
                _ => anyhow::bail!("invalid hex content"),
            }
        };
        Ok((digit(pair[0])? << 4) | digit(pair[1])?)
    }).collect()
}
fn run(args: &[String]) -> anyhow::Result<String> {
    if args[0] == "conformance" {
        return conformance(&args[1..]);
    }
    let (method, path, body) = build_request(args)?;
    let url = format!("http://127.0.0.1:8761/api{path}");
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(300))
        .build()?;
    let mut req = match method.as_str() {
        "GET" => client.get(&url),
        "POST" => client.post(&url),
        "PATCH" => client.patch(&url),
        "DELETE" => client.delete(&url),
        _ => anyhow::bail!("bad method"),
    };
    if !body.is_empty() {
        req = req.header("Content-Type", "application/json");
    }
    let resp = req.body(body).send()?;
    let status = resp.status();
    let text = resp.text()?;
    if !status.is_success() { anyhow::bail!("HTTP {status}: {text}"); }
    if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
        if let (Some(content), Some(output)) = (value.get("content").and_then(serde_json::Value::as_str), value.get("path").and_then(serde_json::Value::as_str)) {
            if output.ends_with(".mid") || output.ends_with(".midi") { std::fs::write(output, decode_hex(content)?)?; }
            else if output.ends_with(".abc") || output.ends_with(".lrc") || output.ends_with(".xml") { std::fs::write(output, content)?; }
        }
    }
    Ok(text)
}

fn build_request(args: &[String]) -> anyhow::Result<(String, String, String)> {
    let mut it = args.iter();
    let cmd = it.next().ok_or_else(|| anyhow::anyhow!("empty"))?.as_str();

    let (method, path, body) = match cmd {
        "voice" => match required(&mut it, "voice <list|inspect|register|remove> ...")?.as_str() {
            "list" => ("GET".into(), "/inference/voices".into(), String::new()),
            "inspect" | "register" => {
                let inspect = args.get(1).map(String::as_str) == Some("inspect");
                let path = required(&mut it, "voice inspect|register <directory> [name]")?;
                let mut body = serde_json::json!({"path":path});
                if let Some(name) = it.next() { body["name"] = serde_json::json!(name); }
                ("POST".into(), if inspect { "/inference/voices/inspect" } else { "/inference/voices" }.into(), body.to_string())
            }
            "remove" => { let id=required(&mut it,"voice remove <id>")?;("DELETE".into(),format!("/inference/voices/{id}"),String::new()) }
            _=>anyhow::bail!("voice <list|inspect|register|remove>"),
        },
        "tempo" => { anyhow::ensure!(required(&mut it,"tempo set <tempos.json>")?=="set","tempo set <tempos.json>");let value=json_file(required(&mut it,"tempo set <tempos.json>")?)?;let tempos=value.as_array().cloned().or_else(||value.get("tempos").and_then(|x|x.as_array()).cloned()).ok_or_else(||anyhow::anyhow!("tempo JSON must be an array or contain tempos[]"))?;("PATCH".into(),"/project/tempos".into(),serde_json::json!({"tempos":tempos}).to_string()) },
        "expression" => {let track=required(&mut it,"expression <track> <expression.json>")?;let value=json_file(required(&mut it,"expression <track> <expression.json>")?)?;anyhow::ensure!(value.is_object(),"expression JSON must be an object");("POST".into(),format!("/track/{track}/expression"),value.to_string())},
        "mixer" => {anyhow::ensure!(required(&mut it,"mixer set <mixer.json>")?=="set","mixer set <mixer.json>");let value=json_file(required(&mut it,"mixer set <mixer.json>")?)?;("PATCH".into(),"/project/mixer".into(),serde_json::json!({"mixer":value}).to_string())},
        "interchange" => {let direction=required(&mut it,"interchange <import|export> <format> <path>")?;let format=required(&mut it,"interchange <import|export> <format> <path>")?;let path=required(&mut it,"interchange <import|export> <format> <path>")?;let action=match(direction.as_str(),format.as_str()){("import","midi")=>"import_midi",("import","musicxml")=>"import_musicxml",("import","ufdata")=>"import_ufdata",("export","midi")=>"export_midi",("export","musicxml")=>"export_musicxml",("export","lrc")=>"export_lrc",_=>anyhow::bail!("unsupported interchange direction/format")};("POST".into(),"/interchange".into(),serde_json::json!({"action":action,"path":path}).to_string())},
        "stems" => {anyhow::ensure!(required(&mut it,"stems export <directory>")?=="export","stems export <directory>");let directory=required(&mut it,"stems export <directory>")?;("POST".into(),"/render/stems".into(),serde_json::json!({"directory":directory}).to_string())},
        "media" => match required(&mut it,"media <import|export|video> ...")?.as_str(){
            "import"=>{let path=required(&mut it,"media import <path> [--track ID] [--tick N]")?;let mut body=serde_json::json!({"path":path});while let Some(flag)=it.next(){let value=required(&mut it,"media import --track ID|--tick N")?;match flag.as_str(){"--track"=>body["track_id"]=serde_json::json!(value),"--tick"=>body["start_tick"]=serde_json::json!(value.parse::<u32>()?),_=>anyhow::bail!("unknown media import option: {flag}")}}("POST".into(),"/media/import".into(),body.to_string())},
            "export"=>{let format=required(&mut it,"media export <fcpxml|aaf> <output-path>")?;let path=required(&mut it,"media export <fcpxml|aaf> <output-path>")?;let route=match format.as_str(){"fcpxml"=>"/media/export/fcpxml","aaf"=>"/media/export/aaf",_=>anyhow::bail!("media export format must be fcpxml or aaf")};("POST".into(),route.into(),serde_json::json!({"output_path":path}).to_string())},
            "video"=>{let video=required(&mut it,"media video <video> <audio> <output>")?;let audio=required(&mut it,"media video <video> <audio> <output>")?;let output=required(&mut it,"media video <video> <audio> <output>")?;("POST".into(),"/media/assemble_video".into(),serde_json::json!({"video_path":video,"audio_path":audio,"output_path":output}).to_string())},
            _=>anyhow::bail!("media <import|export|video>"),
        },
        "music-ai" => {let task=required(&mut it,"music-ai <vocal-to-midi|stems|score|accompaniment> <input>")?;let input=required(&mut it,"music-ai <vocal-to-midi|stems|score|accompaniment> <input>")?;let route=match task.as_str(){"vocal-to-midi"=>"/inference/music-ai/vocal-to-midi","stems"=>"/inference/music-ai/stems","score"=>"/inference/music-ai/score","accompaniment"=>"/inference/music-ai/accompaniment",_=>anyhow::bail!("unknown music-ai operation")};let mut body=if task=="score"||task=="accompaniment"{serde_json::json!({"abc":std::fs::read_to_string(input)?})}else{let bytes=std::fs::read(input)?;serde_json::json!({"audio_base64":base64_encode(&bytes)})};while let Some(flag)=it.next(){let value=required(&mut it,"music-ai --output PATH|--model MODEL|--style STYLE")?;match flag.as_str(){"--output"=>body["output_path"]=serde_json::json!(value),"--model"=>body["model"]=serde_json::json!(value),"--style"=>body["style"]=serde_json::json!(value),_=>anyhow::bail!("unknown music-ai option: {flag}")}}("POST".into(),route.into(),body.to_string())},
        "track-speaker" => { let track=required(&mut it,"track-speaker <track> <speaker-id>")?;let speaker=required(&mut it,"track-speaker <track> <speaker-id>")?;("PATCH".into(),format!("/track/{track}"),serde_json::json!({"voice":{"speaker_id":speaker}}).to_string()) },
        "track-blend" => {let track=required(&mut it,"track-blend <track> <blend.json>")?;let blend=json_file(required(&mut it,"track-blend <track> <blend.json>")?)?;anyhow::ensure!(blend.is_array(),"blend JSON must be an array of speaker_id/weight objects");("PATCH".into(),format!("/track/{track}"),serde_json::json!({"voice":{"blend":blend}}).to_string())},
        "new" => ("POST".to_string(), "/project/new".to_string(), String::new()),
        "ai-lyrics" => {
            // ai-lyrics <主题> <音符数> [--abc 谱面.abc] [--voice Vocal]
            let theme = it.next().ok_or_else(|| anyhow::anyhow!("ai-lyrics <主题> <音符数>"))?.clone();
            let n: u32 = it.next().ok_or_else(|| anyhow::anyhow!("ai-lyrics <主题> <音符数>"))?.parse()?;
            let mut m = serde_json::Map::new();
            m.insert("theme".into(), serde_json::json!(theme));
            m.insert("n_notes".into(), serde_json::json!(n));
            while let Some(k) = it.next() {
                match k.as_str() {
                    "--abc" => { let p = required(&mut it, "ai-lyrics --abc <file>")?; let text = std::fs::read_to_string(p)?; m.insert("abc".into(), serde_json::json!(text)); }
                    "--voice" => { let v = required(&mut it, "ai-lyrics --voice <id>")?; m.insert("voice".into(), serde_json::json!(v)); }
                    _ => {}
                }
            }
            ("POST".to_string(), "/ai/lyrics".to_string(), serde_json::Value::Object(m).to_string())
        }
        "import-abc" => {
            // import-abc <file.abc> [--voice Vocal] [--lyrics 歌词] [--shift N]
            let p = it.next().ok_or_else(|| anyhow::anyhow!("import-abc <file.abc>"))?;
            let text = std::fs::read_to_string(p)?;
            let mut m = serde_json::Map::new();
            m.insert("abc".into(), serde_json::json!(text));
            while let Some(k) = it.next() {
                match k.as_str() {
                    "--voice" => { let v=required(&mut it,"import-abc --voice <id>")?;m.insert("voice".into(),serde_json::json!(v)); }
                    "--lyrics" => { let v=required(&mut it,"import-abc --lyrics <text>")?;m.insert("lyrics".into(),serde_json::json!(v)); }
                    "--shift" => { let v: i32=required(&mut it,"import-abc --shift <N>")?.parse()?;m.insert("key_shift".into(),serde_json::json!(v)); }
                    _ => {}
                }
            }
            ("POST".to_string(), "/import/abc".to_string(), serde_json::Value::Object(m).to_string())
        }
        "open" => {
            let p = it.next().ok_or_else(|| anyhow::anyhow!("open <path>"))?;
            ("POST".to_string(), "/project/open".to_string(), format!(r#"{{"path":"{p}"}}"#))
        }
        "save" => {
            let body = it.next().map(|p| format!(r#"{{"path":"{p}"}}"#)).unwrap_or_default();
            ("POST".to_string(), "/project/save".to_string(), body)
        }
        "track" => match it.next().map(|s| s.as_str()) {
            Some("add") => {
                let kind = it.next().ok_or_else(|| anyhow::anyhow!("track add <kind>"))?;
                let name = it.next().cloned().unwrap_or_default();
                let nb = serde_json::json!({"kind": kind, "name": name});
                ("POST".to_string(), "/track".to_string(), nb.to_string())
            }
            Some("list") => ("GET".to_string(), "/project".to_string(), String::new()),
            Some("remove") => {
                let id = it.next().ok_or_else(|| anyhow::anyhow!("track remove <id>"))?;
                ("DELETE".to_string(), format!("/track/{id}"), String::new())
            }
            Some("set") => {
                let id = it.next().ok_or_else(|| anyhow::anyhow!("track set <id>"))?;
                let mut m = serde_json::Map::new();
                while let Some(k) = it.next() {
                    match k.as_str() {
                        "--vol" => { let v: f32 = required(&mut it, "track set --vol <number>")?.parse()?; m.insert("volume".into(), serde_json::json!(v)); }
                        "--pan" => { let v: f32 = required(&mut it, "track set --pan <number>")?.parse()?; m.insert("pan".into(), serde_json::json!(v)); }
                        "--mute" => { m.insert("mute".into(), serde_json::json!(true)); }
                        "--solo" => { m.insert("solo".into(), serde_json::json!(true)); }
                        "--singer" => { let v = required(&mut it, "track set --singer <id>")?; m.insert("singer_id".into(), serde_json::json!(v)); }
                        _ => {}
                    }
                }
                ("PATCH".to_string(), format!("/track/{id}"), serde_json::Value::Object(m).to_string())
            }
            _ => anyhow::bail!("track <add|list|remove|set>"),
        },
        "note" => match it.next().map(|s| s.as_str()) {
            Some("add") => {
                let track = it.next().ok_or_else(|| anyhow::anyhow!("note add <track>"))?;
                let mut m = serde_json::Map::new();
                while let Some(k) = it.next() {
                    let v = it.next().ok_or_else(|| anyhow::anyhow!("missing value"))?;
                    match k.as_str() {
                        "--tick" => { m.insert("tick".into(), serde_json::json!(v.parse::<u32>()?)); }
                        "--dur" => { m.insert("duration".into(), serde_json::json!(v.parse::<u32>()?)); }
                        "--key" => { m.insert("key".into(), serde_json::json!(v.parse::<u8>()?)); }
                        "--lyric" => { m.insert("lyric".into(), serde_json::json!(v)); }
                        _ => {}
                    }
                }
                ("POST".to_string(), format!("/track/{track}/note"), serde_json::Value::Object(m).to_string())
            }
            Some("list") => {
                let track = it.next().ok_or_else(|| anyhow::anyhow!("note list <track>"))?;
                ("GET".to_string(), format!("/track/{track}/notes"), String::new())
            }
            Some("del") => {
                let track = it.next().ok_or_else(|| anyhow::anyhow!("track"))?;
                let nid = it.next().ok_or_else(|| anyhow::anyhow!("noteId"))?;
                ("DELETE".to_string(), format!("/track/{track}/note/{nid}"), String::new())
            }
            _ => anyhow::bail!("note <add|list|del>"),
        },
        "curve" => match it.next().map(|s| s.as_str()) {
            Some("paint") => {
                let track = it.next().ok_or_else(|| anyhow::anyhow!("track"))?;
                let mut m = serde_json::Map::new();
                while let Some(k) = it.next() {
                    let v = it.next().ok_or_else(|| anyhow::anyhow!("missing value"))?;
                    match k.as_str() {
                        "--param" => { m.insert("param".into(), serde_json::json!(v)); }
                        "--layer" => { m.insert("layer".into(), serde_json::json!(v)); }
                        "--from" => { m.insert("from_tick".into(), serde_json::json!(v.parse::<u32>()?)); }
                        "--to" => { m.insert("to_tick".into(), serde_json::json!(v.parse::<u32>()?)); }
                        "--value" => { m.insert("value".into(), serde_json::json!(v.parse::<f32>()?)); }
                        _ => {}
                    }
                }
                m.entry("layer").or_insert_with(|| serde_json::json!("user"));
                ("POST".to_string(), format!("/track/{track}/curve"), serde_json::Value::Object(m).to_string())
            }
            Some("clear") => {
                let track = it.next().ok_or_else(|| anyhow::anyhow!("track"))?;
                let mut m = serde_json::Map::new();
                while let Some(k) = it.next() {
                    let v = it.next().ok_or_else(|| anyhow::anyhow!("missing value"))?;
                    match k.as_str() {
                        "--param" => { m.insert("param".into(), serde_json::json!(v)); }
                        "--layer" => { m.insert("layer".into(), serde_json::json!(v)); }
                        "--from" => { m.insert("from_tick".into(), serde_json::json!(v.parse::<u32>()?)); }
                        "--to" => { m.insert("to_tick".into(), serde_json::json!(v.parse::<u32>()?)); }
                        _ => {}
                    }
                }
                m.entry("layer").or_insert_with(|| serde_json::json!("user"));
                m.insert("value".into(), serde_json::Value::Null);
                ("POST".to_string(), format!("/track/{track}/curve"), serde_json::Value::Object(m).to_string())
            }
            _ => anyhow::bail!("curve <paint|clear>"),
        },
        "synth" => {
            let body = it.next().map(|t| format!(r#"{{"track":"{t}"}}"#)).unwrap_or_default();
            ("POST".to_string(), "/synth".to_string(), body)
        }
        "transport" => {
            let action = it.next().ok_or_else(|| anyhow::anyhow!("transport play|stop|seek <ms>"))?;
            let body = if action == "seek" {
                let ms: f64 = it.next().ok_or_else(|| anyhow::anyhow!("seek <ms>"))?.parse()?;
                anyhow::ensure!(ms.is_finite() && ms >= 0.0, "seek ms must be nonnegative");
                serde_json::json!({"ms": ms}).to_string()
            } else { String::new() };
            ("POST".to_string(), format!("/transport/{action}"), body)
        }
        "render" => {
            let p: String = it.next().cloned().unwrap_or_else(|| "output.wav".to_string());
            ("POST".to_string(), "/render".to_string(), format!(r#"{{"path":"{p}"}}"#))
        }
        "health" => ("GET".to_string(), "/health".to_string(), String::new()),
        _ => anyhow::bail!("unknown command: {cmd}"),
    };
    Ok((method.into(), path, body))
}

/// 契约对拍：tick→ms（conformance.py 调用）。
fn conformance(args: &[String]) -> anyhow::Result<String> {
    match args[0].as_str() {
        "tick-to-ms" => {
            let tempos_json: Vec<(u32, f64)> = serde_json::from_str(&args[1])?;
            let tick: u32 = args[2].parse()?;
            let tempos: svs_core::timeline::TempoList = tempos_json
                .into_iter()
                .map(|(tick, bpm)| svs_core::timeline::TempoPoint { tick, bpm, curve: svs_core::timeline::TempoCurve::Step })
                .collect();
            let ms = svs_core::timeline::tick_to_ms(&tempos, tick);
            Ok(format!("tick_to_ms:{ms}"))
        }
        "curve-encode" => {
            // 输入 Base64 → 验证解码合法 → 原样返回（roundtrip 由调用方比对）
            let decoded = svs_core::curve::ParamCurve::decode_f32(&args[1]);
            let re = svs_core::curve::ParamCurve::encode_f32(&decoded);
            Ok(format!("curve:{re}"))
        }
        "curve-effective" => {
            let param = args.get(1).ok_or_else(|| anyhow::anyhow!("curve-effective <param> <len> <json ParamCurve>"))?;
            let len: usize = args.get(2).ok_or_else(|| anyhow::anyhow!("missing len"))?.parse()?;
            let curve: svs_core::curve::ParamCurve = serde_json::from_str(
                args.get(3).ok_or_else(|| anyhow::anyhow!("missing ParamCurve JSON"))?,
            )?;
            let values = svs_core::curve::ParamCurve::effective(param, len)(&curve);
            Ok(svs_core::curve::ParamCurve::encode_f32(&values))
        }
        _ => anyhow::bail!("unknown conformance op"),
    }
}
