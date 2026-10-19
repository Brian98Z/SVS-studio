<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { initApiOrigin, wsUrl } from './audio/origin.js'
import { store, actions } from './stores/project.js'
import CurveEditor from './components/CurveEditor.vue'
import TrackPanel from './components/TrackPanel.vue'
import TransportBar from './components/TransportBar.vue'
import TimelineBar from './components/TimelineBar.vue'
import TrackLanes from './components/TrackLanes.vue'
import AppModal from './components/AppModal.vue'
import { ui, uiDone } from './stores/ui.js'
import ToolsPanel from './components/ToolsPanel.vue'
const toolsOpen = ref(false)

const trackKinds = [
  { k: 'sing', label: '歌声轨' }, { k: 'instrument', label: '乐器轨' },
  { k: 'audio', label: '音频轨' }, { k: 'chord', label: '和弦轨' },
]
async function addTrackTop(kind, label) {
  await actions.api('POST', '/track', { kind, name: `${label}${store.project.tracks.length + 1}` })
  await actions.refresh()
}

let ws = null
let reconnectTimer = null
let disposed = false
const connected = ref(false)
function connectWs() {
  ws = new WebSocket(wsUrl('/ws'))
  ws.onopen = () => { connected.value = true }
  ws.onclose = () => {
    connected.value = false
    if (!disposed) reconnectTimer = setTimeout(connectWs, 2000)
  }
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data)
      if (msg.type === 'transport.command') {
        actions.applyRemoteTransport(msg.action, msg.tick)
      } else if (msg.type === 'project.changed') {
        actions.projectChanged()
        actions.refresh().catch(() => {})
      } else if (msg.type === 'project.mixer') {
        actions.refreshMixer().catch(() => {})
      } else if (msg.type === 'engine.status') {
         store.engine.synth_state = msg.synth_state
         store.engine.phrases_done = msg.phrases_done
         store.engine.phrases_total = msg.phrases_total
       }
    } catch { /* 忽略非 JSON 帧 */ }
  }
}

onMounted(async () => { await initApiOrigin(); connectWs(); actions.refresh().catch(() => {}) })
// 自动化测试句柄（生产可留：只读入口）
onMounted(() => { window.__svs = { store, actions } })
// 空格键：播放/暂停切换（DAW 惯例；输入框聚焦时不拦截）
function onKeydown(e) {
  if (e.code !== 'Space') return
  const tag = document.activeElement?.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || document.activeElement?.isContentEditable) return
  e.preventDefault()
  if (store.transport.playing) {
    actions.pause()
  } else {
    actions.play()
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
onUnmounted(() => {
  disposed = true
  clearTimeout(reconnectTimer)
  ws?.close()
  actions.disposePlayback()
})
</script>

<template>
  <div class="shell">
    <TransportBar :connected="connected" />
    <div class="tools-toolbar"><button type="button" @click="toolsOpen = true">Tools</button></div>
    <div class="main">
      <!-- 上区：左轨道头 + 右时间轴（同高对齐） -->
      <div class="top-row">
        <div class="tracks-head-spacer">
          <span class="th-title">轨道</span>
          <span class="th-adds">
            <button v-for="k in trackKinds" :key="k.k" @click="addTrackTop(k.k, k.label)" :title="k.label">+</button>
          </span>
        </div>
        <TimelineBar class="timeline" />
      </div>
      <!-- 中区：左轨道列表 + 右泳道（行高一致逐行对齐） -->
      <div class="mid-row">
        <TrackPanel class="tracks" />
        <TrackLanes class="lanes" />
      </div>
      <!-- 下区：表现力曲线面板（横跨全宽） -->
      <div class="bottom-row">
        <CurveEditor class="curve" />
      </div>
    </div>
    <div class="statusbar">
      <span>{{ store.status }}</span>
      <span v-if="store.engine.synth_state === 'rendering'" class="busy">
        合成中 {{ store.engine.phrases_done }}/{{ store.engine.phrases_total }}
      </span>
      <span class="param-hint">参数通道: {{ store.activeParam }}</span>
    </div>
    <AppModal v-if="ui.dialog" :key="ui.dialog.title + (ui.dialog.label||'')"
      :title="ui.dialog.title" :label="ui.dialog.label" :message="ui.dialog.message"
      :placeholder="ui.dialog.placeholder" :initial="ui.dialog.initial"
      :mode="ui.dialog.mode" :options="ui.dialog.options"
      @done="uiDone" />
    <ToolsPanel v-if="toolsOpen" @close="toolsOpen = false" />
  </div>

</template>
<style>
.shell { display: flex; flex-direction: column; height: 100vh; }
.tools-toolbar { height: 34px; flex: 0 0 34px; display: flex; justify-content: flex-end; align-items: center; padding: 0 10px; background: #181b20; border-bottom: 1px solid #2a2e35; }
.tools-toolbar button { border: 1px solid #3b4350; border-radius: 4px; padding: 5px 12px; color: #e5e7eb; background: #242a32; cursor: pointer; }
.tools-toolbar button:hover { background: #303844; }
.main { flex: 1; display: flex; flex-direction: column; min-height: 0; }
.top-row { display: flex; height: 40px; flex-shrink: 0; }
.tracks-head-spacer { width: 220px; flex-shrink: 0; border-right: 1px solid #2a2e35; background: #181b20;
  display: flex; align-items: center; padding: 0 10px; box-sizing: border-box; }
.th-title { font-size: 13px; color: #9aa3ad; margin-right: auto; }
.th-adds button { background: #262b33; border: 1px solid #343a44; color: #9aa3ad; border-radius: 3px;
  cursor: pointer; margin-left: 3px; padding: 2px 7px; font-size: 12px; }
.timeline { flex: 1; min-width: 0; }
.mid-row { display: flex; flex: 1; min-height: 0; }
.tracks { width: 220px; flex-shrink: 0; border-right: 1px solid #2a2e35; overflow-y: auto; overflow-x: hidden; }
.lanes { flex: 1; min-width: 0; overflow-y: auto; overflow-x: hidden; }
.bottom-row { display: flex; height: 180px; flex-shrink: 0; border-top: 1px solid #2a2e35; }
.curve { flex: 1; min-width: 0; }
.statusbar { height: 26px; display: flex; gap: 16px; align-items: center;
  padding: 0 10px; background: #1b1e24; border-top: 1px solid #2a2e35; font-size: 12px; color: #9aa3ad; }
.busy { color: #e8b74a; }
.param-hint { margin-left: auto; }
</style>
