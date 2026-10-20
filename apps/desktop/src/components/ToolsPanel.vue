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
import SyncPanel from './SyncPanel.vue'

const emit = defineEmits(['close'])
const tabs = [
  { id: 'pronunciation', label: '发音' }, { id: 'voices', label: '声库' }, { id: 'vst', label: 'VST3 效果器' },
  { id: 'expression', label: '音符表现' }, { id: 'tempo', label: '速度图' },
  { id: 'mixer', label: '混音器' }, { id: 'music-ai', label: '音乐 AI' },
  { id: 'media', label: '媒体' }, { id: 'interchange', label: '乐谱导入/导出' }, { id: 'sync', label: '工程同步' },
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
  bundle: { import: 'import_bundle', export: 'export_bundle' },
}
const path = ref('')
const content = ref('')
const projectBase = ref('')
const destination = ref('')
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
      ...(interchangeAction.value === 'import' && format.value !== 'bundle' && content.value ? { content: content.value } : {}),
      ...(format.value === 'bundle' ? { project_base: projectBase.value, destination: destination.value } : {}),
    })
    if (interchangeAction.value === 'import') await actions.refresh()
    message.value = (result.message || (interchangeAction.value === 'import' ? '导入完成。' : '导出完成。')) + (result.warnings?.length ? ` 注意：${result.warnings.join('；')}` : '')
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
        <template v-else-if="active === 'mixer'"><MixerPanel :model-value="mixer" :tracks="mixerTracks" @update:model-value="mixerUpdated" @track-update="trackMixerUpdated" /><p v-if="error" class="error" role="alert">{{ error }}</p></template>
        <MusicAiPanel v-else-if="active === 'music-ai'" />
        <MediaPanel v-else-if="active === 'media'" />
        <SyncPanel v-else-if="active === 'sync'" />
        <section v-else-if="active === 'interchange'" class="interchange">
          <header class="interchange-heading"><div><h2>乐谱导入与导出</h2><p>在工程与常用乐谱格式之间交换内容。</p></div></header>
          <div class="form-row">
            <label>操作<select v-model="interchangeAction"><option value="import">导入到工程</option><option value="export">从工程导出</option></select></label>
            <label>格式<select v-model="format"><option value="musicxml">MusicXML</option><option value="midi">标准 MIDI</option><option value="bundle">SVS 工程包（含音频）</option><option v-if="interchangeAction === 'import'" value="ufdata">UtaFormatix JSON</option><option v-if="interchangeAction === 'export'" value="lrc">LRC 歌词</option><option v-if="interchangeAction === 'export'" value="stems">混音分轨 WAV</option></select></label>
            <label v-if="format === 'bundle' && interchangeAction === 'export'">工程原目录（有相对媒体路径时必填）<input v-model="projectBase" type="text" placeholder="C:\\Music\\MyProject" /></label>
            <label v-if="format === 'bundle' && interchangeAction === 'import'">恢复到新目录（必须尚不存在）<input v-model="destination" type="text" placeholder="C:\\Music\\RestoredProject" /></label>
          </div>
          <div class="source-grid">
            <section v-if="format !== 'stems' || interchangeAction !== 'export'" class="source-panel">
              <label>{{ interchangeAction === 'import' ? '从文件导入' : '导出文件位置' }}<input v-model="path" type="text" :placeholder="format === 'bundle' ? 'C:\\Music\\project.svsbundle' : format === 'midi' ? 'C:\\Music\\score.mid' : format === 'ufdata' ? 'C:\\Music\\score.ufdata' : format === 'lrc' ? 'C:\\Music\\lyrics.lrc' : 'C:\\Music\\score.musicxml'" /></label>
              <p class="field-help">{{ format === 'bundle' ? 'SVS 工程包为 ZIP 容器，建议使用 .svsbundle 后缀；不会覆盖现有文件或恢复目录。' : format === 'midi' ? '支持 .mid、.midi 文件。' : format === 'ufdata' ? '支持 .ufdata、.json 文件。' : format === 'lrc' ? '导出为 .lrc 歌词文件。' : '支持 .musicxml、.xml 文件。' }}</p>
            </section>
            <section v-if="interchangeAction === 'import' && format !== 'midi' && format !== 'bundle'" class="source-panel paste-panel">
              <label>粘贴文件内容（可选）<textarea v-model="content" rows="4" placeholder="粘贴 MusicXML 或 UtaFormatix JSON 内容"></textarea></label>
              <p class="field-help">可填写文件路径，也可直接粘贴内容。</p>
            </section>
            <section v-if="interchangeAction === 'export' && format === 'stems'" class="source-panel">
              <label>分轨输出目录<input v-model="path" type="text" placeholder="C:\\Music\\stems" /></label>
              <p class="field-help">每条音轨将导出为独立 WAV 文件。</p>
            </section>
          </div>
          <p v-if="format === 'bundle'" class="field-help">工程包包含工程快照和引用的外部音频，并校验 SHA-256；不包含声库、模型或插件。导入会替换当前工程，并恢复到新目录；此操作不是云同步。</p>
          <footer class="interchange-footer">
            <button v-if="interchangeAction === 'export' && format === 'stems'" class="primary" :disabled="busy || !path" @click="exportStems">{{ busy ? '处理中…' : '导出当前混音分轨' }}</button>
            <button v-if="format !== 'stems'" class="primary" :disabled="busy || !actionsByFormat[format][interchangeAction] || (format === 'bundle' && (!path || (interchangeAction === 'import' && !destination))) || (!path && !(interchangeAction === 'import' && format !== 'midi' && format !== 'bundle' && content))" @click="runInterchange">{{ busy ? '处理中…' : format === 'bundle' ? (interchangeAction === 'import' ? '恢复工程包' : '导出工程包') : interchangeAction === 'import' ? '导入乐谱' : '导出乐谱' }}</button>
          </footer>
          <p v-if="message" class="success feedback" role="status">{{ message }}</p><p v-if="error" class="error feedback" role="alert">{{ error }}</p>
        </section>
      </div>
    </section>
  </div>
</template>

<style scoped>
.tools-backdrop{position:fixed;inset:0;z-index:100;background:#080a0dcc;display:grid;place-items:center;padding:24px;box-sizing:border-box}.tools-window{width:min(1040px,100%);height:min(760px,100%);display:flex;flex-direction:column;min-height:0;background:#171a20;border:1px solid #363d48;border-radius:10px;box-shadow:0 24px 80px #0009;color:#e5e7eb;font:13px/1.45 system-ui,sans-serif;overflow:hidden}.tools-heading{display:flex;flex-shrink:0;align-items:center;justify-content:space-between;padding:16px 20px 12px;border-bottom:1px solid #2b3039}.tools-heading h1{margin:0;font-size:18px}.tools-heading p{margin:3px 0 0;color:#929baa}.close{font-size:24px;line-height:1;background:transparent;border:0;color:#aab2bf;cursor:pointer}.tools-tabs{display:flex;flex-shrink:0;gap:4px;overflow-x:auto;white-space:nowrap;padding:8px 14px;border-bottom:1px solid #2b3039}.tools-tabs button{flex:0 0 auto;white-space:nowrap;background:transparent;border:0;border-radius:5px;padding:8px 11px;color:#aab2bf;cursor:pointer}.tools-tabs button:hover{background:#242a32;color:#e5e7eb}.tools-tabs button.active{background:#303846;color:#e5e7eb}.tools-content{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;padding:16px;scrollbar-color:#46505e #171a20;scrollbar-width:thin}.tools-content::-webkit-scrollbar{width:10px}.tools-content::-webkit-scrollbar-thumb{background:#46505e;border:2px solid #171a20;border-radius:8px}.tools-content::-webkit-scrollbar-track{background:#171a20}.interchange{width:min(960px,100%);margin:0 auto;display:grid;gap:16px}.interchange-heading{padding-bottom:4px}.interchange h2{margin:0;font-size:18px}.interchange-heading p{margin:4px 0 0;color:#aab2bf}.form-row,.source-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.interchange label{display:grid;gap:6px;color:#c2c9d3;font-weight:550}.interchange input,.interchange select,.interchange textarea{box-sizing:border-box;width:100%;min-width:0;min-height:36px;background:#20252d;border:1px solid #3b4350;border-radius:6px;padding:8px 10px;color:#e5e7eb;font:inherit;font-weight:400}.interchange select{cursor:pointer}.interchange input:focus,.interchange select:focus,.interchange textarea:focus{outline:2px solid #637c9d;outline-offset:1px;border-color:#758eae}.source-panel{min-width:0;padding:14px;background:#1c2027;border:1px solid #303741;border-radius:8px}.source-panel textarea{min-height:104px;max-height:180px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;line-height:1.45}.field-help{margin:8px 0 0;color:#929baa!important;font-size:12px}.interchange-footer{display:flex;justify-content:flex-end;flex-wrap:wrap;gap:8px;padding-top:4px;border-top:1px solid #2b3039}.primary{min-height:36px;background:#d8dce3;color:#171a20;border:0;border-radius:6px;padding:8px 14px;font:inherit;font-weight:650;cursor:pointer;transition:background .15s ease,transform .15s ease}.primary:hover:not(:disabled){background:#fff;transform:translateY(-1px)}.primary:disabled{opacity:.55;cursor:default}.interchange .feedback{padding:9px 11px;border-radius:6px}.success{color:#86d6a2!important;background:#183126}.error{color:#ff9388!important;background:#392321}@media(max-width:600px){.tools-backdrop{padding:8px}.tools-window{height:100%;border-radius:7px}.tools-content{padding:10px}.form-row,.source-grid{grid-template-columns:minmax(0,1fr)}.interchange-footer{justify-content:stretch}.interchange-footer .primary{flex:1}}
</style>