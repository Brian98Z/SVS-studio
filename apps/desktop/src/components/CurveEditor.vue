<script setup>
// 8 通道表现力曲线画笔面板（契约 §2）。
// 视图与钢琴卷帘视口同步（同一 tickX/tickZoom）——所见即所得，点哪画哪。
// 画笔即时本地回显 + 后台批量 POST server。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { store, actions, decodeCurve, PARAMS } from '../stores/project.js'

const canvasRef = ref(null)
let ctx = null
let raf = 0
let painting = false

const COLORS = {
  pitch: '#e05c5c', energy: '#e0a45c', air: '#5cbfe0', tension: '#c9d65c',
  falsetto: '#b48ce0', gender: '#e05ca4', velocity: '#5ce08a', dynamic: '#8899aa',
}
const DESCS = {
  pitch: '音高偏移（音分 ±1200）— 颤音/滑音',
  energy: '发声能量 0-1 — 音量动态',
  air: '气声比例 — 气息感',
  tension: '声带张力 — 声音紧实度',
  falsetto: '假声比例 — 虚/实声转换',
  gender: '共振峰迁移 — 音色明暗',
  velocity: '辅音爆发力 — 咬字冲程',
  dynamic: '主音量包络 — 段落强弱',
}
const nativeVoice = computed(() => /^(diffsinger|voice\.)/.test(store.project.tracks.find(t => t.id === store.selection.trackId)?.singer_id || ''))
const nativeHint = computed(() => !nativeVoice.value ? '' : ['energy', 'tension', 'falsetto', 'dynamic'].includes(store.activeParam)
  ? '原生声库须具备对应条件输入；绮萱不支持此参数，合成将明确报错。'
  : '原生模型控制：Pitch 音分；Air 气声 −96..0dB；Gender 中点0.5；Velocity 中点0.5。')

// 本地缓存按轨道、参数、图层隔离；缺省为 NaN（未设置）。
const localCurves = ref({})
const LAYERS = ['base', 'user', 'envelope', 'direct', 'global']
const LAYER_NAMES = { base: 'Baseline', user: 'User', envelope: 'Envelope', direct: 'Direct', global: 'Global' }
const activeLayer = ref('user')
const showEffective = ref(true)
let cacheEpoch = 0

function cacheKey(trackId = store.selection.trackId, param = store.activeParam, layer = activeLayer.value) {
  return `${trackId}:${param}:${layer}`
}
function songCapacity() {
  const track = store.project.tracks.find(t => t.id === store.selection.trackId)
  const end = (track?.notes || []).reduce((m, n) => Math.max(m, Number(n.tick || 0) + Number(n.duration || n.dur || 0)), 0)
  return Math.max(end + 1, Math.ceil(store.viewport.tickX + (canvasRef.value?.clientWidth || 0) / store.viewport.tickZoom) + 1, 1)
}
function getLocal(trackId = store.selection.trackId, param = store.activeParam, layer = activeLayer.value) {
  const k = cacheKey(trackId, param, layer)
  const track = store.project.tracks.find(t => t.id === trackId)
  const noteEnd = (track?.notes || []).reduce((m, n) => Math.max(m, Number(n.tick || 0) + Number(n.duration || n.dur || 0)), 0)
  const capacity = Math.max(noteEnd + 1, Math.ceil(store.viewport.tickX + (canvasRef.value?.clientWidth || 0) / store.viewport.tickZoom) + 1, 1)
  let arr = localCurves.value[k]
  if (!arr || arr.length < capacity) {
    const next = new Float32Array(capacity).fill(NaN)
    if (arr) next.set(arr)
    localCurves.value[k] = arr = next
  }
  return arr
}
function getEffective(t, trackId = store.selection.trackId, param = store.activeParam) {
  const layer = name => localCurves.value[cacheKey(trackId, param, name)]?.[t]
  const finite = v => Number.isFinite(v) ? v : 0
  const direct = layer('direct')
  let value = Number.isFinite(direct) ? direct : finite(layer('base')) + finite(layer('user')) + finite(layer('envelope'))
  value += finite(layer('global'))
  const pitch = param === 'pitch'
  return Math.max(pitch ? -1200 : 0, Math.min(pitch ? 1200 : 1, value))
}

// ---- 视口同步（与 PianoRoll 相同换算） ----
const KEY_H = () => 14
function tickToX(t, w) {
  return (t - store.viewport.tickX) * store.viewport.tickZoom
}
function xToTick(x, w) {
  return Math.max(0, Math.round(x / store.viewport.tickZoom + store.viewport.tickX))
}

function valueRange(param = store.activeParam, layer = activeLayer.value) {
  if (param === 'pitch') return [-1200, 1200]
  return layer === 'direct' || layer === 'base' || layer === 'effective' ? [0, 1] : [-1, 1]
}
function valueToY(v, h) {
  const [lo, hi] = valueRange()
  return h - (v - lo) / (hi - lo) * (h - 8) - 4
}
function yToValue(y, h) {
  const [lo, hi] = valueRange(strokeTarget?.param || store.activeParam, strokeTarget?.layer || activeLayer.value)
  return (h - 4 - y) / (h - 8) * (hi - lo) + lo
}
function valueToYFor(v, h, param, layer) {
  const [lo, hi] = valueRange(param, layer)
  return h - (v - lo) / (hi - lo) * (h - 8) - 4
}

function draw() {
  const cv = canvasRef.value
  if (!cv) return
  const dpr = window.devicePixelRatio || 1
  const w = cv.clientWidth, h = cv.clientHeight
  if (w === 0 || h === 0) { raf = requestAnimationFrame(draw); return }
  if (cv.width !== w * dpr || cv.height !== h * dpr) {
    cv.width = w * dpr; cv.height = h * dpr
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.fillStyle = '#101216'
  ctx.fillRect(0, 0, w, h)

  const trackId = store.selection.trackId
  const param = store.activeParam
  const layer = activeLayer.value
  const isPitch = param === 'pitch'
  const color = layer === 'base' ? '#a3aab5' : layer === 'effective' ? '#f1d36b' : COLORS[param] || '#5b8def'

  // ---- 横网格 + 值标签 ----
  const [lo, hi] = valueRange(param, layer)
  const lines = isPitch ? [[-1200, '−1200¢'], [-600, '−600¢'], [0, '0'], [600, '+600¢'], [1200, '+1200¢']]
    : lo < 0 ? [[-1, '-1'], [-0.5, '-.5'], [0, '0'], [0.5, '.5'], [1, '1']] : [[0, '0'], [0.25, '.25'], [0.5, '.5'], [0.75, '.75'], [1, '1']]
  ctx.font = '10px sans-serif'
  for (const [v, label] of lines) {
    const y = valueToY(v, h)
    ctx.strokeStyle = v === 0 ? '#2a2e35' : '#1c2026'
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke()
    ctx.fillStyle = '#4b5563'
    ctx.fillText(label, 4, y - 2)
  }

  // ---- 纵网格：与卷帘同一小节对齐 ----
  const ppq = store.project.ppq || 480
  const ts = store.project.time_signatures?.[0] || { tick: 0, numerator: 4, denominator: 4 }
  const barTicks = ppq * ts.numerator * (4 / ts.denominator)
  const [t0, t1] = [store.viewport.tickX, store.viewport.tickX + w / store.viewport.tickZoom]
  ctx.lineWidth = 1
  for (let bar = Math.max(0, Math.floor(t0 / barTicks)); bar * barTicks <= t1; bar++) {
    const x = tickToX(bar * barTicks, w)
    if (x < -2 || x > w + 2) continue
    ctx.strokeStyle = '#20242b'
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke()
    if (x >= 0 && x < w - 20) {
      ctx.fillStyle = '#3d4450'
      ctx.fillText(String(bar + 1), x + 3, h - 4)
    }
  }

  const data = getLocal(trackId, param, layer)
  if (layer === 'effective') {
    const effective = getLocal(trackId, param, 'effective')
    const end = Math.min(effective.length, Math.ceil(t1) + 1)
    for (let t = Math.max(0, Math.floor(t0)); t < end; t++) effective[t] = getEffective(t, trackId, param)
  }
  const tz = store.viewport.tickZoom
  if (tz > 0.5) {
    // 高倍：折线
    ctx.strokeStyle = color
    ctx.lineWidth = 1.6
    ctx.beginPath()
    let started = false
    const startT = Math.max(0, t0), endT = t1
    for (let t = Math.floor(startT); t <= endT; t++) {
      const v = data[t]
      if (Number.isNaN(v)) { started = false; continue }
      const x = tickToX(t, w), y = valueToY(v, h)
      if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
    }
    ctx.stroke()
    // 数据点（高倍时可见）
    if (tz > 2) {
      ctx.fillStyle = color
      for (let t = Math.floor(startT); t <= endT; t++) {
        const v = data[t]
        if (!Number.isNaN(v)) {
          ctx.fillRect(tickToX(t, w) - 1.5, valueToY(v, h) - 1.5, 3, 3)
        }
      }
    }
  } else {
    // 低倍：min/max 包络带（每像素列求值域，半透明填充 + 描边）——密集数据不糊成实心
    const bands = []
    let col = null
    for (let px = 0; px <= w; px++) {
      const t = xToTick(px, w)
      const v = data[t]
      if (v === undefined || Number.isNaN(v)) {
        if (col) { bands.push(col); col = null }
        continue
      }
      if (!col) col = { x0: px, min: v, max: v }
      col.min = Math.min(col.min, v); col.max = Math.max(col.max, v)
      col.x1 = px
    }
    if (col) bands.push(col)
    const isPitch = store.activeParam === 'pitch'
    // 填充带
    ctx.fillStyle = color + '55'
    for (const b of bands) {
      const yTop = valueToY(b.max, h)
      const yBot = valueToY(b.min, h)
      ctx.fillRect(b.x0, yTop, Math.max(b.x1 - b.x0 + 1, 1), Math.max(yBot - yTop, 1.5))
    }
    // 顶边描线
    ctx.strokeStyle = color
    ctx.lineWidth = 1
    ctx.beginPath()
    for (let bi = 0; bi < bands.length; bi++) {
      const b = bands[bi]
      const y = valueToY(b.max, h)
      if (bi === 0) ctx.moveTo(b.x0, y); else ctx.lineTo(b.x0, y)
      ctx.lineTo(b.x1, y)
    }
    ctx.stroke()
  }

  if (showEffective.value && layer !== 'effective') {
    ctx.strokeStyle = '#f1d36b'
    ctx.lineWidth = 1
    ctx.setLineDash([4, 3])
    ctx.beginPath()
    let started = false
    for (let t = Math.floor(Math.max(0, t0)); t <= t1; t++) {
      const present = LAYERS.some(name => Number.isFinite(localCurves.value[cacheKey(trackId, param, name)]?.[t]))
      if (!present) { started = false; continue }
      const x = tickToX(t, w), y = valueToYFor(getEffective(t, trackId, param), h, param, 'effective')
      if (!started) { ctx.moveTo(x, y); started = true } else ctx.lineTo(x, y)
    }
    ctx.stroke(); ctx.setLineDash([])
  }

  // ---- 无内容提示 ----
  let any = false
  for (let t = Math.max(0, Math.floor(t0)); t < t1 && t < data.length; t++) {
    if (!Number.isNaN(data[t])) { any = true; break }
  }
  if (!any) {
    ctx.fillStyle = '#4b5563'
    ctx.fillText(`「${store.activeParam}」当前视口无绘制 — 按住左键拖动画笔（${DESCS[store.activeParam]}）`, 40, h / 2)
  }

  // ---- 通道名水印 ----
  ctx.fillStyle = color + '22'
  ctx.font = 'bold 28px sans-serif'
  ctx.fillText(store.activeParam, w - 130, 34)

  raf = requestAnimationFrame(draw)
}

// ---- 画笔：目标快照 + 有序写入，擦除用 null ----
let pendingPosts = []
let postTimer = null
let strokeTarget = null
let writeTail = Promise.resolve()
const erasing = ref(false)
function flushPending() {
  if (postTimer) { clearTimeout(postTimer); postTimer = null }
  const jobs = pendingPosts
  pendingPosts = []
  writeTail = writeTail.then(async () => {
    for (const job of jobs) {
      try { await actions.paintCurve(job.trackId, job.param, job.from, job.to, job.value, job.layer) }
      catch (error) { store.status = `曲线保存失败：${error.message || error}` }
    }
  })
  return writeTail
}
function schedulePost(target, fromTick, toTick, value) {
  cacheEpoch++
  pendingPosts.push({ ...target, from: fromTick, to: toTick, value })
  if (postTimer) return
  postTimer = setTimeout(flushPending, 200)
}
function onPaint(e) {
  if (!painting || !strokeTarget) return
  const rect = canvasRef.value.getBoundingClientRect()
  const x = e.clientX - rect.left, y = e.clientY - rect.top
  const [lo, hi] = valueRange(strokeTarget.param, strokeTarget.layer)
  const value = erasing.value ? null : Math.max(lo, Math.min(hi, yToValue(y, rect.height)))
  const data = getLocal(strokeTarget.trackId, strokeTarget.param, strokeTarget.layer)
  const brush = Math.max(3, Math.round(6 / store.viewport.tickZoom))
  const centerTick = xToTick(x, rect.width)
  for (let t = centerTick - brush; t <= centerTick + brush; t++) {
    if (t < 0 || t >= data.length) continue
    data[t] = value === null ? NaN : value
  }
  schedulePost(strokeTarget, Math.max(0, centerTick - brush), centerTick + brush + 1, value)
}
function down(e) {
  if (!store.selection.trackId || activeLayer.value === 'base' || activeLayer.value === 'effective') return
  painting = true
  strokeTarget = { trackId: store.selection.trackId, param: store.activeParam, layer: activeLayer.value }
  cacheEpoch++
  onPaint(e)
}
function up() { painting = false; strokeTarget = null; flushPending() }

// 每次请求是完整图层快照；请求序号避免旧响应覆盖新选择。
async function reload() {
  const trackId = store.selection.trackId, param = store.activeParam
  if (!trackId) return
  const epoch = ++cacheEpoch
  try {
    await writeTail
    if (pendingPosts.length) await flushPending()
    const r = await actions.getCurve(trackId, param)
    if (epoch !== cacheEpoch || trackId !== store.selection.trackId || param !== store.activeParam) return
    for (const name of LAYERS) {
      const arr = decodeCurve(r[`${name}_b64`])
      const capacity = Math.max(songCapacity(), arr?.length || 0)
      const local = new Float32Array(capacity).fill(NaN)
      if (arr?.length) local.set(arr)
      localCurves.value[cacheKey(trackId, param, name)] = local
    }
  } catch (error) {
    if (trackId === store.selection.trackId && param === store.activeParam) store.status = `曲线加载失败：${error.message || error}`
  }
}
watch(() => [store.selection.trackId, store.activeParam], reload)
watch(() => store.project.tracks, reload)
watch(activeLayer, reload)

onMounted(() => { ctx = canvasRef.value.getContext('2d'); raf = requestAnimationFrame(draw); reload() })
onUnmounted(() => { cancelAnimationFrame(raf); up() })
</script>

<template>
  <div class="panel">
    <div class="tabs">
      <button v-for="p in PARAMS" :key="p" :class="{ act: store.activeParam === p }"
        :style="{ '--c': COLORS[p] }" :title="DESCS[p]" @click="store.activeParam = p">{{ p }}</button>
      <span class="desc">{{ DESCS[store.activeParam] }}</span>
    </div>
    <div class="layers">
      <button v-for="name in LAYERS" :key="name" :class="{ act: activeLayer === name }"
        @click="activeLayer = name">{{ LAYER_NAMES[name] }}</button>
      <button :class="{ act: activeLayer === 'effective' }" @click="activeLayer = 'effective'">Effective</button>
      <button :class="{ act: erasing }" :disabled="activeLayer === 'base' || activeLayer === 'effective'"
        @click="erasing = !erasing">{{ erasing ? 'Erase: ON' : 'Erase' }}</button>
      <label><input v-model="showEffective" type="checkbox"> Preview</label>
      <span v-if="nativeHint" class="native-hint">{{ nativeHint }}</span>
    </div>
    <canvas ref="canvasRef" class="cv"
      @mousedown="down" @mousemove="onPaint" @mouseup="up" @mouseleave="up" />
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; height: 180px; overflow: hidden; }
.tabs { display: flex; gap: 2px; padding: 4px 6px; background: #1b1e24; align-items: center; }
.tabs button { flex: 1; background: #22262d; color: #9aa3ad; border: none; border-bottom: 2px solid transparent;
  padding: 4px 0; font-size: 11px; cursor: pointer; }
.tabs button.act { color: var(--c); border-bottom-color: var(--c); }
.desc { font-size: 10px; color: #6b7280; margin-left: 8px; white-space: nowrap; }
.layers { display: flex; gap: 4px; padding: 3px 6px; background: #171a20; align-items: center; }
.layers button { background: #22262d; color: #9aa3ad; border: none; padding: 3px 7px; font-size: 10px; cursor: pointer; }
.layers button.act { color: #f1d36b; border-bottom: 1px solid #f1d36b; }
.layers button:disabled { opacity: .45; cursor: default; }
.layers label { color: #7b8490; font-size: 10px; margin-left: auto; white-space: nowrap; }
.native-hint { color: #b6a376; font-size: 10px; max-width: 50%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cv { flex: 1 1 0; width: 100%; min-height: 0; height: 0; cursor: crosshair; display: block; }
</style>
