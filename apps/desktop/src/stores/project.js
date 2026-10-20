// 响应式工程状态（契约 §3 JSON 模型镜像）。
// 单一 store：工程、选区、视口、走带、引擎状态。
import { reactive, watch } from 'vue'
import { PlaybackEngine } from '../audio/playback.js'
import { apiUrl } from '../audio/origin.js'

export const PPQ = 480
export const PARAMS = ['pitch', 'energy', 'air', 'tension', 'falsetto', 'gender', 'velocity', 'dynamic']

export const store = reactive({
  project: {
    schema: 1,
    name: '未命名工程',
    ppq: PPQ,
    sample_rate: 24000,
    tempos: [{ tick: 0, bpm: 120 }],
    time_signatures: [{ tick: 0, numerator: 4, denominator: 4 }],
    tracks: [],
    audio_clips: [],
  },
  path: null,
  selection: { noteIds: [], trackId: null },
  viewport: { tickX: 0, keyY: 72, tickZoom: 0.5, keyZoom: 14 }, // px/tick, px/半音
  transport: { playing: false, tick: 0, ms: 0, metronome: false, state: 'idle', pending: 0, error: '', native_session_id: null, output: 'browser', device_output_id: null, output_device_id: '', output_sample_rate: null },
  audition: { block_frames: 2048, target_blocks: 4 },
  engine: { synth_state: 'idle', phrases_done: 0, phrases_total: 0 },
  activeParam: 'pitch',
  status: '',
})

// ---------- 坐标换算（契约 §1 分段积分，与后端一致） ----------
export function tickToMs(tempos, tick) {
  if (!tempos.length) return 0
  let ms = 0
  for (let i = 0; i < tempos.length; i++) {
    const p = tempos[i], next = tempos[i + 1]
    const end = next ? Math.min(tick, next.tick) : tick
    if (end > p.tick) ms += tempoSegmentMs(p, next, end - p.tick)
    if (!next || tick < next.tick) return ms
  }
  return ms
}

function tempoSegmentMs(point, next, ticks) {
  const factor = 60000 / PPQ
  if (!next || (point.curve || 'step') !== 'linear') return ticks * factor / point.bpm
  const slope = (next.bpm - point.bpm) / (next.tick - point.tick)
  return Math.abs(slope) < 1e-12 ? ticks * factor / point.bpm : factor * Math.log1p(slope * ticks / point.bpm) / slope
}

export function msToTick(tempos, ms) {
  if (!tempos.length || ms <= 0) return 0
  const factor = 60000 / PPQ
  let elapsed = 0
  for (let i = 0; i < tempos.length; i++) {
    const p = tempos[i], next = tempos[i + 1]
    const span = next ? next.tick - p.tick : Infinity
    const duration = tempoSegmentMs(p, next, span)
    if (ms < elapsed + duration) {
      const remaining = ms - elapsed
      let ticks
      if (next && (p.curve || 'step') === 'linear') {
        const slope = (next.bpm - p.bpm) / span
        ticks = Math.abs(slope) < 1e-12 ? remaining * p.bpm / factor : p.bpm * Math.expm1(remaining * slope / factor) / slope
      } else ticks = remaining * p.bpm / factor
      return Math.max(0, Math.min(0xffffffff, Math.round(p.tick + ticks)))
    }
    elapsed += duration
  }
  return tempos.at(-1).tick
}

// ---------- Base64 f32le 曲线解码（契约 §2，零拷贝 Float32Array） ----------
export function decodeCurve(b64) {
  if (!b64) return null
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new Float32Array(bytes.buffer)
}

export function encodeCurve(f32) {
  let s = ''
  const bytes = new Uint8Array(f32.buffer, f32.byteOffset, f32.byteLength)
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i])
  return btoa(s)
}

// ---------- 工程操作（REST） ----------
async function api(method, url, body) {
  const res = await fetch(apiUrl('/api' + url), {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText)
  return data
}

export { api }


const playback = new PlaybackEngine(store, api, tickToMs, msToTick)

watch(() => [store.project.master_volume, store.project.mixer, ...store.project.tracks.map(track =>
  [track.id, track.volume, track.pan, track.mute, track.solo, track.fx, track.sends, track.bus])], () => playback.updateMix(), { deep: true })
export const actions = {
  async refresh() {
    const p = await api('GET', '/project')
    Object.assign(store.project, p)
    // 打开/刷新后自动 fit：视口完整包住全部音符（keyY 定位 + tickZoom 缩放）
    let maxEnd = 0, minKey = 60, maxKey = 72
    for (const t of p.tracks || []) {
      for (const n of t.notes || []) {
        maxEnd = Math.max(maxEnd, n.tick + n.duration)
        minKey = Math.min(minKey, n.key)
        maxKey = Math.max(maxKey, n.key)
      }
    }
    // 默认选中第一个可编辑轨道（详情卷帘/曲线面板依赖）
    if (!store.selection.trackId || !p.tracks?.some(t => t.id === store.selection.trackId)) {
      const firstEditable = p.tracks?.find(t => ['sing', 'instrument'].includes(t.kind))
      if (firstEditable) store.selection.trackId = firstEditable.id
    }
    if (maxEnd > 0) {
      const cv = document.querySelector('canvas.cv')
      const w = cv?.clientWidth || 1200
      const h = cv?.clientHeight || 500
      // tickZoom：全曲宽 fit + 5% 边距；下限保音符可辨（不小于 0.03）
      store.viewport.tickZoom = Math.max(w / (maxEnd * 1.05), 0.004) // 全曲 fit
      store.viewport.tickX = 0
      // keyY：音域居中
      const keySpan = maxKey - minKey + 4
      store.viewport.keyZoom = Math.max(6, Math.min(14, h / keySpan))
      store.viewport.keyY = Math.max(0, (108 - maxKey - 2) * store.viewport.keyZoom)
    }
  },
  async newProject() { playback.stop(); await api('POST', '/project/new'); playback.projectChanged(); await this.refresh() },
  async open(path) { playback.stop(); await api('POST', '/project/open', { path }); playback.projectChanged(); await this.refresh(); store.path = path },
  async save() {
    const r = await api('POST', '/project/save', store.path ? { path: store.path } : {})
    store.path = r.path || store.path
    store.status = `已保存 ${store.path}`
  },
  async addNote(trackId, tick, duration, key, lyric = '') {
    const r = await api('POST', `/track/${trackId}/note`, { tick, duration, key, lyric })
    await this.refresh()
    return r.id
  },
  async updateNote(trackId, noteId, patch) {
    await api('PATCH', `/track/${trackId}/note/${noteId}`, patch)
  },
  async refreshMixer() {
    const project = await api('GET', '/project')
    store.project.master_volume = project.master_volume
    if (project.mixer) store.project.mixer = project.mixer
    const byId = new Map((project.tracks || []).map(track => [track.id, track]))
    for (const track of store.project.tracks) {
      const fresh = byId.get(track.id)
      if (!fresh) continue
      track.volume = fresh.volume
      track.pan = fresh.pan
      track.mute = fresh.mute
      track.solo = fresh.solo
      track.fx = fresh.fx || []
      track.sends = fresh.sends || []
      track.bus = fresh.bus || ''
    }
    playback.mixerRefreshed()
  },
  async deleteNote(trackId, noteId) {
    await api('DELETE', `/track/${trackId}/note/${noteId}`)
  },
  async paintCurve(trackId, param, fromTick, toTick, value, layer = 'user') {
    await api('POST', `/track/${trackId}/curve`, { param, layer, from_tick: fromTick, to_tick: toTick, value })
  },
  async getCurve(trackId, param, len) {
    const query = len == null ? '' : `?len=${len}`
    return api('GET', `/track/${trackId}/curve/${param}${query}`)
  },
  async synth(trackId) {
    store.engine.synth_state = 'rendering'
    try {
      const r = await api('POST', '/synth', trackId ? { track: trackId } : {})
      store.engine.phrases_done = r.phrases_done
      store.engine.phrases_total = r.phrases_total
      store.status = `合成完成 ${r.phrases_done}/${r.phrases_total}（缓存命中 ${r.cache_hits}）`
    } catch (e) {
      store.status = '合成失败: ' + e.message
      throw e
    } finally { store.engine.synth_state = 'idle' }
  },
  play(tick = store.transport.tick) { return playback.play(tick) },
  seek(tick) { return playback.seek(tick) },
  stop() { playback.stop() },
  pause() { playback.pause() },
  applyRemoteTransport(action, tick) { playback.remote(action, tick) },
  projectChanged() { playback.projectChanged() },
  mixerRefreshed() { playback.mixerRefreshed() },
  updateTrackMix(trackId, property, value) {
    const track = store.project.tracks.find(item => item.id === trackId)
    if (!track) return
    track[property] = value
    playback.updateMix()
  },
  async saveTrackMix(trackId, patch) {
    await api('PATCH', `/track/${trackId}`, patch)
  },
  updateMasterVolume(value) {
    store.project.master_volume = value
    playback.updateMix()
  },
  async saveMasterVolume() {
    await api('PATCH', '/project/master', { master_volume: store.project.master_volume })
  },
  disposePlayback() { playback.dispose() },
  async render(path) {
    try {
      store.engine.synth_state = 'rendering'
      const r = await api('POST', '/render', { path })
      store.status = `已导出 ${r.path}`
    } catch (e) {
      store.status = '导出失败: ' + e.message
      throw e
    } finally { store.engine.synth_state = 'idle' }
  },
}
actions.api = api
