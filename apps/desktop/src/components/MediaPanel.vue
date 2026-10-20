<template>
  <section class="media-panel">
    <header><h2>音频与视频</h2><p>导入媒体、查看文件信息，或为视频配乐并导出工程。</p></header>
    <section class="media-section"><h3>媒体导入与探测</h3>
      <div class="path-row"><label for="media-audio">音频文件</label><input id="media-audio" v-model="audioPath" placeholder="输入本地音频完整路径" /><button :disabled="busy || !audioPath" @click="importPath">导入音频</button></div>
      <div class="path-row"><label for="media-probe">媒体文件</label><input id="media-probe" v-model="probePath" placeholder="输入音频或视频完整路径" /><button :disabled="busy || !probePath" @click="probePathNow">探测信息</button></div>
    </section>
    <section class="media-section"><h3>视频配乐</h3><p>保留视频画面，使用指定的音频文件作为配乐。</p>
      <label class="field">视频文件<input v-model="videoPath" placeholder="例如 C:/Music/source.mp4" /></label>
      <label class="field">配乐音频<input v-model="renderedAudioPath" placeholder="输入已渲染音频的完整路径" /></label>
      <label class="field">输出文件<input v-model="videoOutput" placeholder="例如 C:/Music/output.mp4（新文件）" /></label>
      <div class="actions"><button :disabled="busy || !videoPath || !renderedAudioPath || !videoOutput" @click="assemble">合成视频</button></div>
    </section>
    <section class="media-section"><h3>工程交换导出</h3><p>将当前工程交给其他音频或视频编辑软件。</p>
      <label class="field">输出位置<input v-model="outputPath" placeholder="输入新文件的完整路径，包含 .fcpxml 或 .aaf 后缀" /></label>
      <div class="actions"><button :disabled="busy || !outputPath" @click="exportTimeline('fcpxml')">导出 FCPXML</button><button :disabled="busy || !outputPath" @click="exportTimeline('aaf')">导出 AAF</button></div>
    </section>
    <p v-if="message" role="status">{{ message }}</p>
    <pre v-if="metadata">{{ JSON.stringify(metadata, null, 2) }}</pre>
  </section>
</template>

<script setup>
import { ref } from 'vue'
import { apiUrl } from '../audio/origin.js'
const busy = ref(false)
const message = ref('')
const metadata = ref(null)
const audioPath = ref('')
const probePath = ref('')
const videoPath = ref('')
const renderedAudioPath = ref('')
const videoOutput = ref('')
const outputPath = ref('')

async function call(path, body) {
  const response = await fetch(apiUrl(`/api/media/${path}`), {
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
function exportTimeline(format) {
  withBusy(async () => {
    const result = await call(`export/${format}`, { output_path: outputPath.value })
    message.value = `导出完成：${result.path}`
  })
}
</script>

<style scoped>
.media-panel{display:grid;gap:20px;max-width:960px;margin:0 auto;color:#dce4f2;font:13px/1.5 system-ui,sans-serif;min-width:0}
header h2{margin:0;font-size:17px}header p,.media-section>p{margin:5px 0 0;color:#929baa}
.media-section{display:grid;gap:14px;padding:20px;border:1px solid #303742;border-radius:8px;background:#1c2027;min-width:0}.media-section h3{margin:0;font-size:14px}.media-section>p{margin-top:-8px;font-size:12px}
.path-row{display:grid;grid-template-columns:100px minmax(0,1fr) auto;align-items:center;gap:12px}.path-row label,.field{color:#aeb7c5}.field{display:grid;grid-template-columns:100px minmax(0,1fr);align-items:center;gap:12px}
input{box-sizing:border-box;min-width:0;width:100%;height:36px;border:1px solid #3a4351;border-radius:5px;background:#12161d;color:#e5e7eb;padding:7px 10px;font:inherit}input::placeholder{color:#667180}input:focus{outline:2px solid #8b70e8;outline-offset:1px}
.actions{display:flex;gap:8px;padding-left:112px;flex-wrap:wrap}button{min-height:36px;border:1px solid #414b5b;border-radius:5px;background:#2a3240;color:inherit;padding:7px 14px;font:inherit;cursor:pointer;white-space:nowrap}button:hover:not(:disabled){background:#354157}button:disabled{opacity:.45;cursor:default}
p{overflow-wrap:anywhere;margin:0}pre{max-height:240px;overflow:auto;font-size:11px;padding:14px;background:#12161d;border:1px solid #303742;border-radius:6px}
@media(max-width:600px){.media-section{padding:14px}.path-row,.field{grid-template-columns:minmax(0,1fr)}.path-row button{justify-self:start}.actions{padding-left:0}}
</style>
