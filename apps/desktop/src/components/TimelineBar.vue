<script setup>
// 独立时间轴组件：时间刻度（小节号 + 秒）双排标尺 + 播放头 + 点击/拖动跳转播放。
// 与 PianoRoll 共享 store.viewport（tickX/tickZoom）与 store.transport（tick/playing）。
import { onMounted, onUnmounted, ref } from 'vue'
import { store, actions, tickToMs } from '../stores/project.js'

const canvasRef = ref(null)
let ctx = null
let raf = 0
let dragging = false
let lastSeekAt = 0
const HEIGHT = 40 // 与左轨道头 spacer 同高

function fmtTime(ms) {
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
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
  ctx.fillStyle = '#181b20'
  ctx.fillRect(0, 0, w, h)

  const ppq = store.project.ppq || 480
  const tempos = store.project.tempos
  const ts = store.project.time_signatures?.[0] || { tick: 0, numerator: 4, denominator: 4 }
  const barTicks = ppq * ts.numerator * (4 / ts.denominator)
  const tz = store.viewport.tickZoom
  const [t0, t1] = [store.viewport.tickX, store.viewport.tickX + w / tz]

  // ---- 自适应刻度密度 ----
  const barPx = barTicks * tz
  let step = 1                     // 每隔多少小节一个大刻度
  for (const cand of [1, 2, 4, 8, 16, 32]) {
    if (barTicks * cand * tz >= 56) { step = cand; break }
    step = cand
  }

  ctx.font = '10px sans-serif'
  const firstBar = Math.max(0, Math.floor(t0 / barTicks))
  const lastBar = Math.ceil(t1 / barTicks)
  for (let bar = Math.floor(firstBar / step) * step; bar <= lastBar + step; bar += step) {
    const tickPos = bar * barTicks
    const x = (tickPos - store.viewport.tickX) * tz
    if (x < -40 || x > w + 10) continue
    // 大刻度线
    ctx.strokeStyle = '#343a44'
    ctx.beginPath(); ctx.moveTo(x + 0.5, 22); ctx.lineTo(x + 0.5, h); ctx.stroke()
    // 上排：小节号 + 时间
    ctx.fillStyle = '#9aa3ad'
    ctx.fillText(String(bar + 1), x + 4, 12)
    const ms = tickToMs(tempos, tickPos)
    ctx.fillStyle = '#5b6472'
    ctx.fillText(fmtTime(ms), x + 4, 24)
  }
  // 中刻度（拍）——仅在密度足够时
  if (barPx >= 90) {
    ctx.strokeStyle = '#23272e'
    for (let bar = firstBar; bar <= lastBar + 1; bar++) {
      for (let b = 1; b < ts.numerator; b++) {
        const x = ((bar * barTicks + b * ppq) - store.viewport.tickX) * tz
        if (x < 0 || x > w) continue
        ctx.beginPath(); ctx.moveTo(x + 0.5, h - 8); ctx.lineTo(x + 0.5, h); ctx.stroke()
      }
    }
  }

  // ---- 播放头 ----
  const px = (store.transport.tick - store.viewport.tickX) * tz
  if (px >= 0 && px <= w) {
    // 播放头时间气泡
    const label = fmtTime(tickToMs(tempos, store.transport.tick))
    ctx.fillStyle = '#e8b74a'
    ctx.fillRect(px - 18, 0, 36, 14)
    ctx.fillStyle = '#14161a'
    ctx.font = 'bold 10px sans-serif'
    ctx.fillText(label, px - 15, 10)
    // 竖线（延伸到下方卷帘视觉贯通）
    ctx.strokeStyle = '#e8b74a'
    ctx.lineWidth = 1.5
    ctx.beginPath(); ctx.moveTo(px, 14); ctx.lineTo(px, h); ctx.stroke()
    // 手柄三角
    ctx.fillStyle = '#e8b74a'
    ctx.beginPath()
    ctx.moveTo(px - 5, 14); ctx.lineTo(px + 5, 14); ctx.lineTo(px, 21)
    ctx.closePath(); ctx.fill()
  }
  raf = requestAnimationFrame(draw)
}

function tickFromEvent(e) {
  const rect = canvasRef.value.getBoundingClientRect()
  const x = e.clientX - rect.left
  return Math.max(0, Math.round(x / store.viewport.tickZoom + store.viewport.tickX))
}

function doSeek(tick) {
  actions.seek(tick)
}

function onMouseDown(e) {
  dragging = true
  doSeek(tickFromEvent(e))
}
function onMouseMove(e) {
  if (!dragging) return
  const tick = tickFromEvent(e)
  const now = performance.now()
  // 拖动节流 150ms：防止音频重启过频
  if (now - lastSeekAt > 150) {
    lastSeekAt = now
    doSeek(tick)
  } else {
    store.transport.tick = tick // 视觉即时跟随
  }
}
function onMouseUp(e) {
  if (dragging && e) doSeek(tickFromEvent(e))
  dragging = false
}
function onLeave(e) {
  if (dragging && e) doSeek(tickFromEvent(e))
  dragging = false
}

onMounted(() => { ctx = canvasRef.value.getContext('2d'); raf = requestAnimationFrame(draw) })
onUnmounted(() => cancelAnimationFrame(raf))
</script>

<template>
  <canvas ref="canvasRef" class="timeline"
    :style="{ height: HEIGHT + 'px' }"
    @mousedown="onMouseDown" @mousemove="onMouseMove"
    @mouseup="onMouseUp" @mouseleave="onLeave"
    title="点击/拖动：跳转播放位置" />
</template>

<style scoped>
.timeline { width: 100%; display: block; cursor: ew-resize; }
</style>
