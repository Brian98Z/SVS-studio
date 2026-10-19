<script setup>
// 分轨泳道视图：左侧轨道行 <-> 右侧泳道一一横向对齐(SoundBug/DAW 排列视图)。
// 每轨泳道内音符按该轨音高范围压缩绘制(mini-roll);点击泳道选中轨,双击进详情卷帘编辑。
// 顶部时间轴(TimelineBar)与泳道共用视口 tickX/tickZoom。
// 滚轮：Ctrl+滚轮以鼠标为锚缩放时间刻度；普通滚轮横向滚动。
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { store, api, actions } from '../stores/project.js'
import { trackRowH } from '../stores/layout.js'

const canvasRef = ref(null)
let ctx = null
let raf = 0
// M/S 徽标命中区（每次 draw 重算）：{x, y, w, h, track, kind}
let badgeRects = []
const lanesHeight = computed(() =>
  store.project.tracks.reduce((s, t) => s + trackRowH(t), 0))
// 拖拽平移状态（左键按住拖动 = 移动视口；移动 <4px 视为点击选轨）
const drag = reactive({ active: false, moved: false, startX: 0, tickX0: 0 })

function draw() {
  const cv = canvasRef.value
  if (!cv) { raf = requestAnimationFrame(draw); return }
  const dpr = window.devicePixelRatio || 1
  const w = cv.clientWidth, h = cv.clientHeight
  if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.fillStyle = '#12151a'
  ctx.fillRect(0, 0, w, h)
  badgeRects = []
  const tX = store.viewport.tickX
  const tz = store.viewport.tickZoom
  const tracks = store.project.tracks
  let yCursor = 0
  for (let i = 0; i < tracks.length; i++) {
    const track = tracks[i]
    const y0 = yCursor
    const rowH = trackRowH(track)
    yCursor += rowH
    ctx.fillStyle = i % 2 ? '#141820' : '#12151a'
    ctx.fillRect(0, y0, w, rowH)
    ctx.strokeStyle = '#23272e'
    ctx.strokeRect(0.5, y0 + 0.5, w - 1, rowH - 1)
    const notes = track.notes || []
    const sel = track.id === store.selection.trackId
    // ---- M/S 徽标（行左上角）----
    const bs = 18 // 徽标边长
    for (const [bi, kind] of [['M', 'mute'], ['S', 'solo']].entries()) {
      // entries() 给 [index, pair]；解构出 label/field
      const [label, field] = kind
      const bx = 6 + bi * 22
      const by = y0 + 5
      const on = field === 'mute' ? track.mute : track.solo
      ctx.fillStyle = on ? (field === 'mute' ? '#c0392b' : '#e67e22') : '#262b33'
      ctx.strokeStyle = '#3a4150'
      ctx.beginPath()
      ctx.roundRect(bx, by, bs, bs, 4)
      ctx.fill(); ctx.stroke()
      ctx.fillStyle = on ? '#fff' : '#7a828e'
      ctx.font = 'bold 10px sans-serif'
      ctx.textBaseline = 'middle'
      ctx.textAlign = 'center'
      ctx.fillText(label, bx + bs / 2, by + bs / 2 + 1)
      ctx.textAlign = 'left'
      ctx.textBaseline = 'alphabetic'
      badgeRects.push({ x: bx, y: by, w: bs, h: bs, trackId: track.id, kind: field })
    }
    if (notes.length && track.kind !== 'chord') {
      ctx.save()
      ctx.beginPath(); ctx.rect(0, y0, w, rowH); ctx.clip()
      let lo = 127, hi = 0
      for (const n of notes) { lo = Math.min(lo, n.key); hi = Math.max(hi, n.key) }
      const isSing = track.kind === 'sing'
      const span = Math.max(hi - lo, 12)
      const pad = isSing ? 6 : 4
      const laneH = rowH - pad * 2
      // 人声轨：底部留 16px 歌词条；旋律块画在上部 laneH 内
      const lyricH = isSing ? 16 : 0
      const laneH2 = laneH - lyricH
      for (const n of notes) {
        const x = (n.tick - tX) * tz
        const nw = Math.max(n.duration * tz, 3)
        if (x + nw < 0 || x > w) continue
        const rel = (hi - n.key) / span
        const ny = y0 + pad + rel * laneH2
        const nh = Math.max(laneH2 / span, 3)
        ctx.fillStyle = isSing ? (sel ? '#5b8def' : '#3d5a8a')
          : sel ? '#4a9d8f' : '#2e5a52'
        ctx.fillRect(x + 0.5, ny, nw - 1, nh)
        if (!isSing && n.lyric && nw > 20 && laneH2 / span >= 8) {
          ctx.fillStyle = '#c8cdd4'
          ctx.font = '9px sans-serif'
          ctx.fillText(n.lyric, x + 2, ny + nh - 1)
        }
      }
      // 人声轨歌词条：文字按音符起点排布；挤不下时隔字显示（保证可读）
      if (isSing) {
        ctx.font = 'bold 12px "Microsoft YaHei", sans-serif'
        ctx.textBaseline = 'middle'
        let lastRight = -999
        for (const n of notes) {
          if (!n.lyric || n.lyric === 'a') continue
          const x = (n.tick - tX) * tz
          if (x < -20 || x > w + 20) continue
          const tw = ctx.measureText(n.lyric).width
          if (x < lastRight + 2) continue // 与前一个字重叠则跳过（缩小时自动抽稀）
          ctx.fillStyle = sel ? '#eaf1ff' : '#aab6c8'
          ctx.fillText(n.lyric, x + 2, y0 + rowH - 9)
          lastRight = x + tw
        }
        ctx.textBaseline = 'alphabetic'
      }
      ctx.restore()
    } else if (track.kind === 'chord') {
      ctx.save()
      ctx.beginPath(); ctx.rect(0, y0, w, rowH); ctx.clip()
      ctx.font = '11px sans-serif'
      for (const n of notes) {
        const x = (n.tick - tX) * tz
        if (x < -60 || x > w) continue
        ctx.fillStyle = '#8b7bd8'
        ctx.fillText(n.lyric || '', x + 5, y0 + rowH / 2 + 4)
      }
      ctx.restore()
    }
  }
  // 播放头（贯穿全部泳道）
  const px = (store.transport.tick - tX) * tz
  if (px >= 0 && px <= w) {
    ctx.strokeStyle = '#e8b74a'
    ctx.lineWidth = 1.5
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, yCursor); ctx.stroke()
  }
  raf = requestAnimationFrame(draw)
}

function onWheel(e) {
  e.preventDefault()
  const rect = canvasRef.value.getBoundingClientRect()
  const mx = e.clientX - rect.left
  const tz0 = store.viewport.tickZoom
  const tX0 = store.viewport.tickX
  const tickAtMouse = tX0 + mx / tz0
  if (e.ctrlKey || e.metaKey) {
    // 缩放：每档 ±15%，0.002（全曲级）到 2（音符级）；锚点保持
    const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15
    const tz1 = Math.min(2, Math.max(0.002, tz0 * factor))
    store.viewport.tickZoom = tz1
    store.viewport.tickX = Math.max(0, tickAtMouse - mx / tz1)
  } else {
    // 横向平移：上限 = 曲长 + 2 屏，防止拖到无限空白
    let maxEnd = 0
    for (const t of store.project.tracks) for (const n of t.notes || []) maxEnd = Math.max(maxEnd, n.tick + n.duration)
    const maxX = maxEnd + 2 * (window.innerWidth || 1500) / tz0
    store.viewport.tickX = Math.min(maxX, Math.max(0, tX0 + (e.deltaY + e.deltaX) / tz0))
  }
}

function pickTrack(e) {
  const rect = canvasRef.value.getBoundingClientRect()
  let y = e.clientY - rect.top
  for (const track of store.project.tracks) {
    const rh = trackRowH(track)
    if (y < rh) return track
    y -= rh
  }
  return null
}

function onMouseDown(e) {
  if (e.button !== 0) return
  drag.active = true
  drag.moved = false
  drag.startX = e.clientX
  drag.tickX0 = store.viewport.tickX
}
function onMouseMove(e) {
  if (!drag.active) return
  const dx = e.clientX - drag.startX
  if (Math.abs(dx) > 4) drag.moved = true
  if (drag.moved) {
    // 拖拽 = 视口平移（跟手：鼠标移动 dx 像素，视口左移 dx/tz tick）
    const tz = store.viewport.tickZoom
    let maxEnd = 0
    for (const t of store.project.tracks) for (const n of t.notes || []) maxEnd = Math.max(maxEnd, n.tick + n.duration)
    const maxX = maxEnd + 2 * (window.innerWidth || 1500) / tz
    store.viewport.tickX = Math.min(maxX, Math.max(0, drag.tickX0 - dx / tz))
    canvasRef.value.style.cursor = 'grabbing'
  }
}
function onMouseUp(e) {
  if (!drag.active) return
  drag.active = false
  canvasRef.value.style.cursor = 'grab'
  if (!drag.moved) {
    // 未拖动 = 点击：先判 M/S 徽标
    const rect = canvasRef.value.getBoundingClientRect()
    const px = e.clientX - rect.left, py = e.clientY - rect.top
    const hit = badgeRects.find(b => px >= b.x && px <= b.x + b.w && py >= b.y && py <= b.y + b.h)
    if (hit) {
      const track = store.project.tracks.find(t => t.id === hit.trackId)
      if (!track) return
      const body = hit.kind === 'mute' ? { mute: !track.mute } : { solo: !track.solo }
      api('PATCH', `/track/${track.id}`, body).then(() => actions.refresh())
      return
    }
    // 否则选轨
    const track = pickTrack(e)
    if (track) store.selection.trackId = track.id
  }
}

onMounted(() => {
  ctx = canvasRef.value.getContext('2d')
  raf = requestAnimationFrame(draw)
  canvasRef.value.addEventListener('wheel', onWheel, { passive: false })
  canvasRef.value.addEventListener('mousedown', onMouseDown)
  // Home 键：视口回起点
  window.addEventListener('keydown', onKey)
  window.addEventListener('mousemove', onMouseMove)
  window.addEventListener('mouseup', onMouseUp)
  canvasRef.value.style.cursor = 'grab'
})
function onKey(e) {
  if (e.key === 'Home' && !(e.target instanceof HTMLInputElement)) {
    store.viewport.tickX = 0
  }
}
onUnmounted(() => {
  cancelAnimationFrame(raf)
  canvasRef.value?.removeEventListener('wheel', onWheel)
  canvasRef.value?.removeEventListener('mousedown', onMouseDown)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
  window.removeEventListener('keydown', onKey)
})
</script>


<template>
  <canvas ref="canvasRef" class="lanes"
    :style="{ height: lanesHeight + 'px' }"
    />
</template>

<style scoped>
.lanes { width: 100%; display: block; cursor: pointer; }
</style>
