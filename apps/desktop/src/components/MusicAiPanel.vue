<script setup>
import { onMounted, ref } from 'vue'
import { apiUrl } from '../audio/origin.js'
import { store, msToTick, api, actions } from '../stores/project.js'
const status = ref(null)
const mode = ref('score')
const abc = ref('')
const theme = ref('')
const bars = ref(8)
const bpm = ref(120)
const key = ref('C')
const meter = ref('4/4')
const audio = ref(null)
const style = ref('piano accompaniment')
const statusError = ref('')
const stemUrls = ref([])
const busy = ref(false)
const error = ref('')
const result = ref(null)
const modes = [
  { id: 'score', label: '作曲' }, { id: 'accompaniment', label: '伴奏' },
  { id: 'stems', label: '分轨' }, { id: 'vocal-to-midi', label: '人声转 MIDI' },
]
async function call(path, body) {
  const response = await fetch(apiUrl(path), {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
  return data
}
onMounted(async () => {
  try { status.value = await call('/api/inference/music-ai/status') }
  catch (e) { statusError.value = e.message }
})
async function audioPayload() {
  if (!audio.value) throw new Error('请先选择 WAV 音频文件。')
  const bytes = new Uint8Array(await audio.value.arrayBuffer())
  let binary = ''
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return { audio_base64: btoa(binary) }
}
async function submit() {
  error.value = ''; result.value = null; busy.value = true
  try {
    const payload = mode.value === 'score'
      ? (theme.value.trim() ? { theme: theme.value.trim(), bars: Number(bars.value), bpm: Number(bpm.value), key: key.value, meter: meter.value } : { abc: abc.value })
      : mode.value === 'accompaniment' ? { abc: abc.value, style: style.value } : await audioPayload()
    const response = await call(`/api/inference/music-ai/${mode.value}`, payload)
    if (mode.value === 'score' && response.notes?.length) {
      const created = await api('POST', '/track', { kind: 'sing', name: 'AI 作曲' })
      const trackId = created.id || created.track?.id
      if (!trackId) throw new Error('工程未返回新音符轨道 ID。')
      for (const note of response.notes) await api('POST', `/track/${encodeURIComponent(trackId)}/note`, { tick: note.tick, duration: note.duration, key: note.key, velocity: note.velocity ?? 0.8, lyric: note.lyric || 'a', legato: Boolean(note.legato) })
      await api('PATCH', '/project/tempos', { tempos: [{ tick: 0, bpm: response.bpm || 120, curve: 'step' }] })
      await actions.refresh()
      result.value = { engine: response.engine, device: response.device, providers: response.providers, bpm: response.bpm, bars: response.bars, note_count: response.note_count, imported_track_id: trackId }
    } else if (mode.value === 'accompaniment' && response.notes?.length) {
      const created = await api('POST', '/track', { kind: 'instrument', name: 'AI 伴奏' })
      const trackId = created.id || created.track?.id
      if (!trackId) throw new Error('工程未返回伴奏轨道 ID。')
      for (const note of response.notes) await api('POST', `/track/${encodeURIComponent(trackId)}/note`, { tick: note.tick, duration: note.duration, key: note.key, velocity: note.velocity ?? 0.8, lyric: '', legato: Boolean(note.legato) })
      await actions.refresh()
      result.value = { engine: response.engine, device: response.device, providers: response.providers, note_count: response.note_count, imported_track_id: trackId }
    } else if (mode.value === 'accompaniment') {
      throw new Error('伴奏服务未返回可导入音符。')
    } else if (mode.value === 'vocal-to-midi' && response.notes?.length) {
      const created = await api('POST', '/track', { kind: 'instrument', name: 'Basic Pitch 转录' })
      const trackId = created.id || created.track?.id
      if (!trackId) throw new Error('工程未返回新音符轨道 ID。')
      const tempos = store.project.tempos || [{ tick: 0, bpm: response.tempo_bpm || 120 }]
      for (const note of response.notes) {
        const secondsAtStart = note.tick / ((response.ppq || 480) * (response.tempo_bpm || 120) / 60)
        const secondsAtEnd = (note.tick + note.duration) / ((response.ppq || 480) * (response.tempo_bpm || 120) / 60)
        const tick = msToTick(tempos, secondsAtStart * 1000)
        const endTick = msToTick(tempos, secondsAtEnd * 1000)
        await api('POST', `/track/${encodeURIComponent(trackId)}/note`, { tick, duration: Math.max(1, endTick - tick), key: note.key, velocity: note.velocity ?? 0.8, lyric: note.lyric || 'a', legato: Boolean(note.legato) })
      }
      await actions.refresh()
      result.value = { engine: response.engine, device: response.device, providers: response.providers, note_count: response.note_count, imported_track_id: trackId }
    } else if (mode.value === 'stems') {
      const stems = []
      for (const [name, stem] of Object.entries(response.stems || {})) {
        const raw = atob(stem.audio_base64)
        const bytes = new Uint8Array(raw.length)
        for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
        const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/wav' }))
        stemUrls.value.push({ name, url })
        stems.push(name)
      }
      result.value = { engine: response.engine, device: response.device, sample_rate: response.sample_rate, stem_count: stems.length }
    } else result.value = { engine: response.engine, device: response.device, providers: response.providers, note_count: response.note_count }
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
function changeMode(id) {
  stemUrls.value.forEach(({ url }) => URL.revokeObjectURL(url))
  stemUrls.value = []; mode.value = id; error.value = ''; result.value = null
}
</script>

<template>
  <section class="music-ai" aria-label="音乐 AI 工具">
    <header><div><h2>音乐 AI</h2><p v-if="status">服务：{{ Object.values(status.features || {}).some(feature => feature.available) ? '部分可用' : '当前不可用' }}</p><p v-else>服务状态尚未取得</p><p v-if="status?.features?.vocal_to_midi?.available">人声转 MIDI GPU：{{ status.features.vocal_to_midi.gpu }}</p><p v-else-if="status?.features?.vocal_to_midi?.prerequisite" class="error">{{ status.features.vocal_to_midi.prerequisite }}</p><p v-if="status?.features?.stem_splitter?.available">分轨 GPU：{{ status.features.stem_splitter.gpu }}</p><p v-else-if="status?.features?.stem_splitter?.prerequisite" class="error">{{ status.features.stem_splitter.prerequisite }}</p><p v-if="statusError" class="error">{{ statusError }}</p></div></header>
    <nav><button v-for="item in modes" :key="item.id" :class="{active: mode === item.id}" @click="changeMode(item.id)">{{ item.label }}</button></nav>
    <template v-if="mode === 'score'">
      <label>主题（填写则生成新曲；留空则编排下方 ABC）<input v-model="theme" type="text" placeholder="例如：晨光中的远行" /></label>
      <div v-if="theme.trim()" class="fields"><label>小节数<input v-model.number="bars" type="number" min="1" max="64" /></label><label>速度 BPM<input v-model.number="bpm" type="number" min="30" max="300" /></label><label>调号<input v-model="key" maxlength="8" /></label><label>拍号<input v-model="meter" placeholder="4/4" /></label></div>
      <label v-else>ABC 乐谱<textarea v-model="abc" rows="10" placeholder="X:1&#10;T:示例&#10;M:4/4&#10;L:1/8&#10;K:C&#10;CDEF GABc|"></textarea></label>
    </template>
    <template v-else-if="mode === 'accompaniment'"><label>源 ABC 乐谱<textarea v-model="abc" rows="10" placeholder="粘贴包含旋律声部的完整 ABC 乐谱"></textarea></label><label>伴奏风格<input v-model="style" type="text" maxlength="200" /></label></template>
    <label v-else>输入音频（WAV）<input type="file" accept="audio/wav,.wav" @change="audio = $event.target.files?.[0] || null"></label>
    <button class="primary" :disabled="busy || ((mode === 'score' && !theme.trim() && !abc.trim()) || (mode === 'accompaniment' && !abc.trim()) || ((mode === 'stems' || mode === 'vocal-to-midi') && !audio))" @click="submit">{{ busy ? '处理中…' : '运行并导入结果' }}</button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="result" class="result" role="status">{{ result.engine }}<span v-if="result.device"> · {{ result.device }}</span><span v-if="result.providers"> · {{ result.providers.join(', ') }}</span> · {{ result.note_count ?? result.stem_count }} 项{{ result.imported_track_id ? ' · 已导入工程' : '' }}</p>
    <ul v-if="stemUrls.length" class="downloads"><li v-for="stem in stemUrls" :key="stem.name"><a :href="stem.url" :download="`${stem.name}.wav`">下载 {{ stem.name }}.wav</a></li></ul>
  </section>
</template>

<style scoped>
.music-ai{display:grid;gap:14px;max-width:820px;color:#e5e7eb}.music-ai header h2{margin:0;font-size:16px}.music-ai p{margin:3px 0;color:#aab2bf}.music-ai nav{display:flex;gap:6px;flex-wrap:wrap}.music-ai button{background:#242a32;border:1px solid #3b4350;color:#d5d9df;border-radius:5px;padding:7px 10px;cursor:pointer}.music-ai nav button.active{background:#394354;color:#fff}.music-ai label{display:grid;gap:6px;color:#aab2bf}.music-ai textarea{background:#20252d;border:1px solid #3b4350;border-radius:5px;padding:10px;color:#e5e7eb;font:13px/1.5 ui-monospace,monospace;resize:vertical}.music-ai .primary{justify-self:start;background:#d8dce3;color:#171a20;font-weight:600}.music-ai .primary:disabled{opacity:.55;cursor:default}.error{color:#ff9388!important}.result{white-space:pre-wrap;overflow:auto;max-height:320px;padding:12px;background:#20252d;border-radius:5px;color:#b9c7db}
</style>