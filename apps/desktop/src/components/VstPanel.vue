<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { apiUrl } from '../audio/origin.js'
import EffectParameterGrid from './EffectParameterGrid.vue'
const props = defineProps({ endpoint: { type: String, default: '/api/inference/vst' } })
const plugins = ref([])
const selectedPath = ref('')
const parameters = ref([])
const busy = ref(false)
const error = ref('')
const message = ref('')
const audioFile = ref(null)
const outputUrl = ref('')
const sampleRate = ref(48000)
const blockFrames = ref(4096)
const tailSeconds = ref(0)
const editor = ref(null)
const presetState = ref(null)
let editorTimer = null
let disposed = false
let editorPath = ''
onUnmounted(() => {
  disposed = true
  clearTimeout(editorTimer)
  if (editor.value && !['completed', 'failed'].includes(editor.value.state)) call('/editor', { action: 'close', id: editor.value.id }).catch(() => {})
})
async function pollEditor() {
  if (disposed || !editor.value) return
  try {
    const result = await call('/editor', { action: 'status', id: editor.value.id })
    if (disposed) return
    editor.value = result
    if (result.state === 'completed') {
      if (selectedPath.value === editorPath) {
        parameters.value = (result.parameters || []).map(parameter => ({ ...parameter, next: parameter.value }))
        presetState.value = result.state_base64
        message.value = '原生编辑器已关闭，参数与插件预设状态已读回；可处理音频。未修改工程轨道。'
      }
      return
    }
    if (result.state === 'failed') { error.value = result.error; return }
  } catch (cause) { error.value = cause.message; if (!disposed) editorTimer = setTimeout(pollEditor, 1500); return }
  editorTimer = setTimeout(pollEditor, 700)
}
async function openEditor() {
  busy.value = true; error.value = ''
  editorPath = selectedPath.value
  try {
    editor.value = await call('/editor', { action: 'open', path: editorPath,
      parameters: Object.fromEntries(parameters.value.map(parameter => [parameter.name, parameter.next])), state_base64: presetState.value })
    message.value = '独立原生窗口正在打开；关闭后回读参数。不是正在试听的轨道实例。'
    if (disposed) { await call('/editor', { action: 'close', id: editor.value.id }); return }
    pollEditor()
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function closeEditor() {
  try { await call('/editor', { action: 'close', id: editor.value.id }) }
  catch (cause) { error.value = cause.message }
}
const editorActive = computed(() => editor.value && !['completed', 'failed'].includes(editor.value.state))
const manualPath = ref('plugins/')
function selectManualPath() { if (!manualPath.value.trim()) return; selectedPath.value = manualPath.value.trim(); parameters.value = []; error.value = ''; message.value = '' }
const chosen = computed(() => plugins.value.find(plugin => plugin.path === selectedPath.value))
watch(selectedPath, () => { parameters.value = []; presetState.value = null; error.value = ''; message.value = '' })
async function call(path, body) {
  const base = props.endpoint.replace(/\/$/, '')
  const response = await fetch(apiUrl(`${base}${path}`), {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(result.detail || result.error || response.statusText)
  return result
}

onMounted(() => { discover() })
async function discover() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await call('/discover')
    plugins.value = result.plugins || []
    selectedPath.value = plugins.value[0]?.path || ''
    parameters.value = []
    message.value = `${plugins.value.length} 个项目内插件路径已发现；发现过程未加载插件。`
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

function updateParameter(name, value) { const parameter = parameters.value.find(item => item.name === name); if (parameter) parameter.next = value }
async function inspect() {
  if (!selectedPath.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await call('/inspect', { path: selectedPath.value })
    parameters.value = (result.parameters || []).map(parameter => ({ ...parameter, next: parameter.value ?? (parameter.type === 'boolean' ? false : parameter.type === 'string' ? '' : 0) }))
    presetState.value = null
    message.value = '插件已显式加载以读取参数；可打开独立原生编辑器，关闭后回读参数。'
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

function base64ToBytes(value) {
  const raw = atob(value); const bytes = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}
function bytesToBase64(bytes) {
  let raw = ''
  for (let i = 0; i < bytes.length; i += 0x8000) raw += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(raw)
}

async function processFile() {
  if (!selectedPath.value || !audioFile.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const context = new AudioContext({ sampleRate: Number(sampleRate.value) })
    try {
      const decoded = await context.decodeAudioData(await audioFile.value.arrayBuffer())
      if (decoded.numberOfChannels < 1) throw new Error('音频文件没有可用声道。')
      const frames = decoded.length
      const interleaved = new Float32Array(frames * 2)
      const left = decoded.getChannelData(0)
      const right = decoded.numberOfChannels > 1 ? decoded.getChannelData(1) : left
      for (let frame = 0; frame < frames; frame++) { interleaved[frame * 2] = left[frame]; interleaved[frame * 2 + 1] = right[frame] }
      const result = await call('/process', {
        path: selectedPath.value,
        sample_rate: decoded.sampleRate,
        block_frames: Number(blockFrames.value),
        parameters: Object.fromEntries(parameters.value.map(parameter => [parameter.name, parameter.next])),
        state_base64: presetState.value,
        tail_seconds: Number(tailSeconds.value),
        audio_base64: bytesToBase64(new Uint8Array(interleaved.buffer)),
      })
      const data = base64ToBytes(result.audio_base64)
      const floats = new Float32Array(data.buffer, data.byteOffset, data.byteLength / 4)
      const output = new AudioBuffer({ numberOfChannels: 2, length: result.frames, sampleRate: result.sample_rate })
      const outLeft = output.getChannelData(0); const outRight = output.getChannelData(1)
      for (let frame = 0; frame < result.frames; frame++) { outLeft[frame] = floats[frame * 2]; outRight[frame] = floats[frame * 2 + 1] }
      const blob = await audioBufferToWav(output)
      if (outputUrl.value) URL.revokeObjectURL(outputUrl.value)
      outputUrl.value = URL.createObjectURL(blob)
      message.value = `VST3 实际处理完成：${result.frames} 帧，${result.sample_rate} Hz 立体声。`
    } finally { await context.close() }
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function audioBufferToWav(buffer) {
  const channels = 2, frames = buffer.length
  const bytes = new ArrayBuffer(44 + frames * channels * 4), view = new DataView(bytes)
  const text = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)) }
  text(0, 'RIFF'); view.setUint32(4, 36 + frames * channels * 4, true); text(8, 'WAVE'); text(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 3, true); view.setUint16(22, channels, true)
  view.setUint32(24, buffer.sampleRate, true); view.setUint32(28, buffer.sampleRate * channels * 4, true)
  view.setUint16(32, channels * 4, true); view.setUint16(34, 32, true); text(36, 'data'); view.setUint32(40, frames * channels * 4, true)
  const left = buffer.getChannelData(0), right = buffer.getChannelData(1)
  for (let i = 0, offset = 44; i < frames; i++) { view.setFloat32(offset, left[i], true); offset += 4; view.setFloat32(offset, right[i], true); offset += 4 }
  return new Blob([bytes], { type: 'audio/wav' })
}
</script>

<template>
  <section class="vst-panel">
    <header><div><h2>VST3 效果器</h2><p>离线立体声处理 · 读取参数、打开编辑器或处理时执行可信插件原生代码</p></div><button :disabled="busy || editorActive" @click="discover">{{ busy ? '处理中…' : '扫描路径' }}</button></header>
    <div class="plugin-row">
      <label class="manual-path">插件路径（相对 plugins/） <input v-model="manualPath" type="text" placeholder="plugins/Effect.vst3"></label>
      <button :disabled="busy || !manualPath.trim()" @click="selectManualPath">使用此路径</button>
      <select v-model="selectedPath" :disabled="busy">
        <option value="">选择项目内插件</option>
        <option v-for="plugin in plugins" :key="plugin.path" :value="plugin.path">{{ plugin.name }} — {{ plugin.path }}</option>
      </select>
      <button :disabled="busy || !selectedPath || editorActive" @click="inspect">加载并读取参数</button>
      <button :disabled="busy || !selectedPath || editorActive" @click="openEditor">打开原生编辑器</button>
      <button v-if="editorActive" @click="closeEditor">关闭编辑器并读回</button>
    </div>
    <p v-if="chosen" class="path">已选路径：{{ chosen.path }}</p>
    <p v-if="selectedPath && !chosen" class="path">手动选择：{{ selectedPath }}</p>
    <EffectParameterGrid v-if="parameters.length" :parameters="parameters" @change="updateParameter" />
    <div class="process-row">
      <label>输入音频 <input type="file" accept="audio/*,.wav,.flac,.mp3,.ogg" @change="audioFile = $event.target.files?.[0] || null"></label>
      <label>尾音秒数 (0–10) <input v-model.number="tailSeconds" type="number" min="0" max="10" step="0.1"></label>
      <label>分块帧数 <input v-model.number="blockFrames" type="number" min="1" max="65536" step="1"></label>
      <button :disabled="busy || editorActive || !selectedPath || !audioFile" @click="processFile">处理并导出 WAV</button>
    </div>
    <audio v-if="outputUrl" :src="outputUrl" controls />
    <a v-if="outputUrl" :href="outputUrl" download="vst3-processed.wav">下载处理结果</a>
    <p v-if="message" class="message">{{ message }}</p><p v-if="error" class="error">{{ error }}</p>
    <p class="warning">仅加载可信插件，原生代码在隔离进程运行。编辑器为工具页独立实例，关闭后回读可枚举参数；不嵌入应用，不联动实时轨道，不保留插件未暴露的内部状态。离开工具页关闭编辑器，失去连接30秒后自动关闭。</p>
  </section>
</template>

<style scoped>
.vst-panel{display:grid;gap:12px;min-width:0;padding:16px;background:#181b20;color:#d5d9df;font:12px/1.45 system-ui,sans-serif}.vst-panel header,.plugin-row,.process-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;min-width:0}.vst-panel h2{margin:0;font-size:15px;color:#f3f4f6}.vst-panel p{margin:3px 0;color:#9aa3af}.vst-panel button,.vst-panel input[type=number],.manual-path input,.process-row input[type=file]{box-sizing:border-box;padding:6px 9px;border:1px solid #38404b;border-radius:4px;background:#242a32;color:#e5e7eb}.vst-panel button{cursor:pointer}.vst-panel button:disabled{opacity:.45;cursor:default}.manual-path{display:grid;gap:4px;min-width:min(260px,100%);flex:1}.plugin-row select{box-sizing:border-box;width:min(340px,100%);height:32px;min-width:0;max-width:100%;padding:4px 8px;border:1px solid #38404b;border-radius:4px;background:#242a32;color:#e5e7eb}.path{overflow-wrap:anywhere}.process-row label{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap}.vst-panel audio{width:100%}.vst-panel a{color:#9ab9ff}.message{color:#a5d6a7!important}.error{color:#ff8c82!important}.warning{padding-top:8px;border-top:1px solid #303640;color:#d6b86c!important}
.plugin-row{align-items:flex-end}.plugin-row input,.plugin-row button{height:32px}
</style>
