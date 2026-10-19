<script setup>
import { computed, ref } from 'vue'

const props = defineProps({ endpoint: { type: String, default: '/api/vst' } })
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
const chosen = computed(() => plugins.value.find(plugin => plugin.path === selectedPath.value))

async function call(path, body) {
  const base = props.endpoint.replace(/\/$/, '')
  const response = await fetch(`${base}${path}`, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(result.detail || result.error || response.statusText)
  return result
}

async function discover() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await call('/vst/discover')
    plugins.value = result.plugins || []
    selectedPath.value = ''
    parameters.value = []
    message.value = `${plugins.value.length} 个 bundle 路径已发现；目录名不是插件元数据，发现过程未加载插件。`
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function inspect() {
  if (!selectedPath.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await call('/vst/inspect', { path: selectedPath.value })
    parameters.value = (result.parameters || []).map(parameter => ({ ...parameter, next: parameter.value ?? 0 }))
    message.value = '插件已显式加载以读取参数；原生插件代码可能执行。编辑器 GUI 不支持。'
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
      const result = await call('/vst/process', {
        path: selectedPath.value,
        sample_rate: decoded.sampleRate,
        block_frames: Number(blockFrames.value),
        parameters: Object.fromEntries(parameters.value.map(parameter => [parameter.name, Number(parameter.next)])),
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
    <header><div><h2>VST3 效果器</h2><p>离线立体声处理 · 插件原生代码仅在明确检查或处理时加载</p></div><button :disabled="busy" @click="discover">{{ busy ? '处理中…' : '扫描路径' }}</button></header>
    <div class="plugin-row">
      <select v-model="selectedPath" :disabled="busy">
        <option value="">选择已发现的 .vst3 bundle</option>
        <option v-for="plugin in plugins" :key="plugin.path" :value="plugin.path">{{ plugin.name }} — {{ plugin.path }}</option>
      </select>
      <button :disabled="busy || !selectedPath" @click="inspect">加载并读取参数</button>
    </div>
    <p v-if="chosen" class="path">已选路径：{{ chosen.path }}</p>
    <div v-if="parameters.length" class="parameters">
      <label v-for="parameter in parameters" :key="parameter.name" class="parameter">
        <span>{{ parameter.name }} <small v-if="parameter.min_value !== undefined">{{ parameter.min_value }} – {{ parameter.max_value }}</small></span>
        <input v-if="parameter.min_value !== undefined && parameter.max_value !== undefined && parameter.max_value > parameter.min_value" v-model.number="parameter.next" type="range" :min="parameter.min_value" :max="parameter.max_value" step="any">
        <input v-else v-model.number="parameter.next" type="number" step="any">
        <output>{{ parameter.next }}</output>
      </label>
    </div>
    <div class="process-row">
      <label>输入音频 <input type="file" accept="audio/*,.wav,.flac,.mp3,.ogg" @change="audioFile = $event.target.files?.[0] || null"></label>
      <label>分块帧数 <input v-model.number="blockFrames" type="number" min="1" max="65536" step="1"></label>
      <button :disabled="busy || !selectedPath || !audioFile" @click="processFile">处理并导出 WAV</button>
    </div>
    <audio v-if="outputUrl" :src="outputUrl" controls />
    <a v-if="outputUrl" :href="outputUrl" download="vst3-processed.wav">下载处理结果</a>
    <p v-if="message" class="message">{{ message }}</p><p v-if="error" class="error">{{ error }}</p>
    <p class="warning">插件扫描仅返回 bundle 路径，不代表它是效果器。检查/处理会运行所选插件的原生代码；仅加载可信插件。插件编辑器 GUI 不支持。</p>
  </section>
</template>

<style scoped>
.vst-panel{display:grid;gap:12px;padding:16px;background:#181b20;color:#d5d9df;font:12px/1.45 system-ui,sans-serif;overflow:auto}.vst-panel header,.plugin-row,.process-row{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.vst-panel h2{margin:0;font-size:15px;color:#f3f4f6}.vst-panel p{margin:3px 0;color:#9aa3af}.vst-panel button,.vst-panel select,.vst-panel input[type=number]{padding:6px 9px;border:1px solid #38404b;border-radius:4px;background:#242a32;color:#e5e7eb}.vst-panel button{cursor:pointer}.vst-panel button:disabled{opacity:.45;cursor:default}.plugin-row select{min-width:min(520px,70vw);max-width:100%}.path{overflow-wrap:anywhere}.parameters{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:8px}.parameter{display:grid;grid-template-columns:1fr auto;gap:4px 8px;padding:8px;background:#20252c;border-radius:4px}.parameter small{display:block;color:#87909c}.parameter input{grid-column:1}.parameter output{grid-column:2;grid-row:2;align-self:center;font-variant-numeric:tabular-nums}.process-row label{display:flex;align-items:center;gap:6px}.vst-panel audio{width:100%}.vst-panel a{color:#9ab9ff}.message{color:#a5d6a7!important}.error{color:#ff8c82!important}.warning{padding-top:8px;border-top:1px solid #303640;color:#d6b86c!important}
</style>
