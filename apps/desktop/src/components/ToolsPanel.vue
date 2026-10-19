<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { apiUrl } from '../audio/origin.js'
import { store, actions } from '../stores/project.js'
import { defaultMixerConfig } from '../audio/fx.js'
import VoiceManager from './VoiceManager.vue'
import VstPanel from './VstPanel.vue'
import ExpressionPanel from './ExpressionPanel.vue'
import TempoEditor from './TempoEditor.vue'
import MixerPanel from './MixerPanel.vue'
import MusicAiPanel from './MusicAiPanel.vue'
import MediaPanel from './MediaPanel.vue'
import PronunciationPanel from './PronunciationPanel.vue'

const emit = defineEmits(['close'])
const tabs = [
  { id: 'pronunciation', label: '发音' }, { id: 'voices', label: '声库' }, { id: 'vst', label: 'VST3 效果器' },
  { id: 'expression', label: '音符表现' }, { id: 'tempo', label: '速度图' },
  { id: 'mixer', label: '混音器' }, { id: 'music-ai', label: '音乐 AI' },
  { id: 'media', label: '媒体' }, { id: 'interchange', label: '乐谱导入/导出' },
]
const active = ref('pronunciation')
const mixer = computed({ get: () => store.project.mixer || defaultMixerConfig(), set: value => { store.project.mixer = value } })
const mixerTracks = computed(() => store.project.tracks)
const interchangeAction = ref('import')
const format = ref('musicxml')
const actionsByFormat = {
  musicxml: { import: 'import_musicxml', export: 'export_musicxml' },
  midi: { import: 'import_midi', export: 'export_midi' },
  ufdata: { import: 'import_ufdata', export: null },
  lrc: { import: null, export: 'export_lrc' },
  stems: { import: null, export: null },
}
const path = ref('')
const content = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')

async function request(pathname, body) {
  const response = await fetch(apiUrl(pathname), {
    method: body ? 'POST' : 'GET', headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(result.detail || result.error || response.statusText)
  return result
}
async function runInterchange() {
  busy.value = true; message.value = ''; error.value = ''
  if (!actionsByFormat[format.value][interchangeAction.value]) {
    error.value = '所选格式不支持此操作。'
    busy.value = false
    return
  }
  try {
    const result = await request('/api/interchange', {
      action: actionsByFormat[format.value][interchangeAction.value], path: path.value,
      ...(interchangeAction.value === 'import' && content.value ? { content: content.value } : {}),
    })
    if (interchangeAction.value === 'import') await actions.refresh()
    message.value = result.message || (interchangeAction.value === 'import' ? '导入完成。' : '导出完成。')
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function tempoSaved(tempos) {
  store.project.tempos = tempos
  await actions.refresh()
}
async function exportStems() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    if (!path.value) throw new Error('请填写分轨输出目录。')
    const result = await request('/api/render/stems', { directory: path.value })
    message.value = result.message || `分轨已导出至 ${result.directory || path.value}`
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
async function mixerUpdated(value) {
  error.value = ''
  try {
    const result = await actions.api('PATCH', '/project/mixer', { mixer: value })
    store.project.mixer = result.mixer || value
    if (result.master_volume !== undefined) store.project.master_volume = result.master_volume
    await actions.refreshMixer()
  } catch (cause) { error.value = cause.message }
}
async function trackMixerUpdated({ id, patch }) {
  error.value = ''
  try {
    const update = { ...patch }
    if (patch.effects) { update.fx = patch.effects; delete update.effects }
    await actions.api('PATCH', `/track/${encodeURIComponent(id)}`, update)
    const track = store.project.tracks.find(item => item.id === id)
    if (track) Object.assign(track, update, patch.effects ? { effects: patch.effects } : {})
    await actions.refreshMixer()
  } catch (cause) { error.value = cause.message }
}
onMounted(async () => {
  try { await actions.refresh() } catch (cause) { error.value = cause.message }
})

</script>
<template>
  <div class="tools-backdrop" @click.self="emit('close')">
    <section class="tools-window" role="dialog" aria-modal="true" aria-label="Tools">
      <header class="tools-heading"><div><h1>工具</h1><p>工程实用工具与集成</p></div><button class="close" aria-label="关闭工具" @click="emit('close')">×</button></header>
      <nav class="tools-tabs" aria-label="Tool categories">
        <button v-for="tab in tabs" :key="tab.id" :class="{ active: active === tab.id }" @click="active = tab.id">{{ tab.label }}</button>
      </nav>
      <div class="tools-content">
        <PronunciationPanel v-if="active === 'pronunciation'" />
        <VoiceManager v-else-if="active === 'voices'" />
        <VstPanel v-else-if="active === 'vst'" endpoint="/api/inference/vst" />
        <ExpressionPanel v-else-if="active === 'expression'" />
        <TempoEditor v-else-if="active === 'tempo'" :tempos="store.project.tempos" @saved="tempoSaved" />
        <MixerPanel v-else-if="active === 'mixer'" :model-value="mixer" :tracks="mixerTracks" @update:model-value="mixerUpdated" @track-update="trackMixerUpdated" />
        <p v-if="active === 'mixer' && error" class="error" role="alert">{{ error }}</p>
        <MusicAiPanel v-else-if="active === 'music-ai'" />
        <MediaPanel v-else-if="active === 'media'" />
        <section v-else-if="active === 'interchange'" class="interchange">
          <h2>乐谱导入与导出</h2>
          <p>选择受支持的交换格式和文件路径；文本格式也可直接粘贴内容。</p>
          <div class="form-row"><label>操作<select v-model="interchangeAction"><option value="import">导入到工程</option><option value="export">导出工程</option></select></label>
            <label>格式<select v-model="format"><option value="musicxml">MusicXML</option><option value="midi">标准 MIDI</option><option v-if="interchangeAction === 'import'" value="ufdata">UtaFormatix JSON</option><option v-if="interchangeAction === 'export'" value="lrc">LRC 歌词</option><option v-if="interchangeAction === 'export'" value="stems">混音分轨 WAV</option></select></label></div>
          <label v-if="format !== 'stems' || interchangeAction !== 'export'">文件路径<input v-model="path" type="text" :placeholder="format === 'midi' ? 'C:\\Music\\score.mid' : 'C:\\Music\\score.musicxml'" /></label>
          <label v-if="interchangeAction === 'import' && format !== 'midi'">或粘贴文件内容<textarea v-model="content" rows="8" placeholder="粘贴 MusicXML 或 UtaFormatix JSON"></textarea></label>
          <label v-if="interchangeAction === 'export' && format === 'stems'">分轨输出目录<input v-model="path" type="text" placeholder="C:\\Music\\stems" /></label>
          <button v-if="interchangeAction === 'export' && format === 'stems'" class="primary" :disabled="busy || !path" @click="exportStems">导出当前混音分轨</button>
          <button class="primary" :disabled="busy || !actionsByFormat[format][interchangeAction] || (!path && !(interchangeAction === 'import' && format !== 'midi' && content))" @click="runInterchange">{{ busy ? '处理中…' : interchangeAction === 'import' ? '导入乐谱' : '导出乐谱' }}</button>
          <p v-if="message" class="success" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
        </section>
      </div>
    </section>
  </div>
</template>

<style scoped>
.tools-backdrop{position:fixed;inset:0;z-index:100;background:#080a0dcc;display:grid;place-items:center;padding:24px;box-sizing:border-box}.tools-window{width:min(1040px,100%);height:min(760px,100%);display:flex;flex-direction:column;min-height:0;background:#171a20;border:1px solid #363d48;border-radius:10px;box-shadow:0 24px 80px #0009;color:#e5e7eb;font:13px/1.45 system-ui,sans-serif;overflow:hidden}.tools-heading{display:flex;align-items:center;justify-content:space-between;padding:16px 20px 12px;border-bottom:1px solid #2b3039}.tools-heading h1{margin:0;font-size:18px}.tools-heading p{margin:3px 0 0;color:#929baa}.close{font-size:24px;line-height:1;background:transparent;border:0;color:#aab2bf;cursor:pointer}.tools-tabs{display:flex;gap:4px;overflow-x:auto;padding:8px 14px;border-bottom:1px solid #2b3039}.tools-tabs button{white-space:nowrap;background:transparent;border:0;border-radius:5px;padding:8px 11px;color:#aab2bf;cursor:pointer}.tools-tabs button:hover{background:#242a32;color:#e5e7eb}.tools-tabs button.active{background:#303846;color:#e5e7eb}.tools-content{min-height:0;overflow:auto;padding:16px}.interchange{max-width:720px;display:grid;gap:14px}.interchange h2{margin:0;font-size:16px}.interchange p{margin:0;color:#aab2bf}.form-row{display:flex;gap:12px}.interchange label{display:grid;gap:5px;color:#aab2bf}.interchange input,.interchange select,.interchange textarea{min-width:0;background:#20252d;border:1px solid #3b4350;border-radius:5px;padding:8px;color:#e5e7eb;font:inherit}.form-row label{flex:1}.primary{justify-self:start;background:#d8dce3;color:#171a20;border:0;border-radius:5px;padding:8px 14px;font:inherit;font-weight:600;cursor:pointer}.primary:disabled{opacity:.55;cursor:default}.success{color:#86d6a2!important}.error{color:#ff9388!important}@media(max-width:600px){.tools-backdrop{padding:8px}.tools-window{height:100%;border-radius:7px}.tools-content{padding:10px}.form-row{flex-direction:column}}
</style>