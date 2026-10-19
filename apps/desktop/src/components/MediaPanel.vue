<template>
  <section class="media-panel">
    <h3>音频 / 视频</h3>
    <label>导入音频路径 <input v-model="audioPath" placeholder="本地绝对路径" /><button :disabled="busy || !audioPath" @click="importPath">导入音频</button></label>
    <label>探测媒体路径 <input v-model="probePath" placeholder="本地绝对路径" /><button :disabled="busy || !probePath" @click="probePathNow">探测</button></label>
    <label>视频路径 <input v-model="videoPath" placeholder="本地视频绝对路径" /></label>
    <label>配乐音频路径 <input v-model="renderedAudioPath" placeholder="已渲染 score/audio 绝对路径" /></label>
    <label>视频输出路径 <input v-model="videoOutput" placeholder="新建 output.mp4" /></label>
    <div class="actions"><button :disabled="busy || !videoPath || !renderedAudioPath || !videoOutput" @click="assemble">合成视频</button></div>
    <label>导出位置 <input v-model="outputPath" placeholder="新文件的本地绝对路径" /></label>
    <div class="actions">
      <button :disabled="busy || !outputPath" @click="exportTimeline('fcpxml')">导出 FCPXML</button>
      <button :disabled="busy || !outputPath" @click="exportTimeline('aaf')">导出 AAF</button>
    </div>
    <p v-if="message" role="status">{{ message }}</p>
    <pre v-if="metadata">{{ JSON.stringify(metadata, null, 2) }}</pre>
  </section>
</template>

<script setup>
import { ref } from 'vue'
const busy = ref(false)
const message = ref('')
const metadata = ref(null)
const audioPath = ref('')
const probePath = ref('')
const videoPath = ref('')
const renderedAudioPath = ref('')
const videoOutput = ref('')

async function call(path, body) {
  const response = await fetch(`/api/media/${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
  return data
}
async function withBusy(work) {
  busy.value = true; message.value = ''
  try { await work() } catch (error) { message.value = error.message || String(error) }
  finally { busy.value = false }
}
function importPath() {
  withBusy(async () => {
    const result = await call('import', { path: audioPath.value })
    message.value = `已导入，片段 ${result.clip.id}`
    metadata.value = result.probe
  })
}
function probePathNow() {
  withBusy(async () => { metadata.value = await call('probe', { path: probePath.value }); message.value = '探测完成' })
}
function assemble() {
  withBusy(async () => {
    const result = await call('assemble_video', { video_path: videoPath.value, audio_path: renderedAudioPath.value, output_path: videoOutput.value })
    message.value = `视频合成完成：${result.path}`
  })
}
function importFile(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (file?.path) { audioPath.value = file.path; importPath() }
  else if (file) message.value = '浏览器无法读取本地绝对路径；请在路径框中输入文件路径。'
}
function probeFile(event) {
  const file = event.target.files?.[0]; event.target.value = ''
  if (file?.path) { probePath.value = file.path; probePathNow() }
  else if (file) message.value = '浏览器无法读取本地绝对路径；请在路径框中输入文件路径。'
}
function exportTimeline(format) {
  withBusy(async () => {
    const result = await call(`export/${format}`, { output_path: outputPath.value })
    message.value = `导出完成：${result.path}`
  })
}
</script>

<style scoped>
.media-panel { display:grid; gap:10px; padding:12px; color:#dce4f2; background:#171a22; border-radius:8px }
.media-panel label { display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:12px }
.actions { display:flex; gap:8px }
button { border:1px solid #414858; border-radius:5px; background:#252b37; color:inherit; padding:6px 10px; cursor:pointer }
button:disabled { opacity:.5; cursor:default }
p { overflow-wrap:anywhere; font-size:12px }
pre { max-height:180px; overflow:auto; font-size:11px }
</style>
