<script setup>
import { ref, watch } from 'vue'
import { store, actions, api } from '../stores/project.js'
import { uiPrompt, uiConfirm, uiSelect } from '../stores/ui.js'

defineProps({ connected: { type: Boolean, default: false } })

import { computed } from 'vue'
const bpm = computed(() => store.project.tempos?.[0]?.bpm ?? 120)
const outputDevices = ref([])
const loadingDevices = ref(false)
const devicesError = ref('')
async function refreshOutputDevices() {
  loadingDevices.value = true
  devicesError.value = ''
  try {
    const result = await api('GET', '/audio/output/devices')
    outputDevices.value = result.devices
    updateOutputDevice()
  } catch (error) { devicesError.value = error.message }
  finally { loadingDevices.value = false }
}
function updateOutputDevice() {
  const selected = store.transport.output_device_id
  const device = outputDevices.value.find(d => selected ? d.id === selected : d.is_default)
  store.transport.output_sample_rate = device?.sample_rate || null
  if (selected && !device) devicesError.value = '所选设备已不可用，请重新选择；不会自动切换输出。'
}
async function chooseOutputDevice(event) {
  const id = event.target.value
  const previous = store.transport.output_device_id
  const device = outputDevices.value.find(d => id ? d.id === id : d.is_default)
  devicesError.value = ''
  if (!device?.selectable) { event.target.value = previous; devicesError.value = '设备不可用；请刷新设备。'; return }
  if (!store.transport.playing) {
    store.transport.output_device_id = id
    updateOutputDevice()
    return
  }
  loadingDevices.value = true
  try { await actions.switchOutputDevice(device.id, device.sample_rate) }
  catch (error) { devicesError.value = error.message }
  finally { event.target.value = store.transport.output_device_id; loadingDevices.value = false }
}
watch(() => store.transport.output, value => { if (value === 'device') refreshOutputDevices() })

async function togglePlay() {
  const state = store.transport.state
  if (state === 'buffering') actions.pause()
  else if (state === 'playing') actions.stop()
  else await actions.play()
}
function setMasterVolume(event) {
  actions.updateMasterVolume(Number(event.target.value))
}
async function saveMasterVolume() {
  try { await actions.saveMasterVolume() } catch (error) { store.status = `混音保存失败：${error.message}` }
}
async function newProject() {
  if (confirm('新建工程（当前未保存内容丢失）？')) await actions.newProject()
}
async function saveProject() { await actions.save() }
async function openProject() {
  const path = await uiPrompt({ title: '打开工程', label: '工程文件路径 (.svsproj)：',
    initial: 'projects/demo.svsproj' })
  if (path) await actions.open(path)
}
async function renderWav() {
  const path = await uiPrompt({ title: '导出 WAV', label: '输出文件路径：',
    initial: 'output.wav' })
  if (path) await actions.render(path)
}

// ---- ABC 曲谱导入 ----
const abcLyrics = ref('')
async function importAbc() {
  const inp = document.createElement('input')
  inp.type = 'file'
  inp.accept = '.abc,.txt'
  inp.onchange = async () => {
    const file = inp.files?.[0]
    if (!file) return
    const abc = await file.text()
    await doImportAbc(abc, file.name)
  }
  inp.click()
}
/** 歌词三选：1=AI 生成 / 2=手动输入 / 回车=啊占位。返回 null=用户取消。 */
async function askLyrics(abc, voice, nNotes) {
  const mode = await uiSelect({ title: '歌词方式', message: `本声部共 ${nNotes} 个音符，每个音符唱一个字。`,
    options: [
      { value: 'ai', label: 'AI 生成歌词（输入主题，自动按字数与乐句填写）' },
      { value: 'manual', label: '手动输入歌词' },
      { value: 'ah', label: '不填歌词（全程「啊」试唱）' },
    ] })
  if (mode === null) return null
  if (mode === 'ai') {
    const theme = await uiPrompt({ title: 'AI 生成歌词', label: `主题（将生成恰好 ${nNotes} 字）：`,
      placeholder: '例如：夏夜的海边、失恋后的地铁站' })
    if (theme === null || !theme.trim()) return null
    store.status = `AI 生成歌词中（${nNotes} 字，约 10-30s）…`
    try {
      const r = await api('POST', '/ai/lyrics', { theme: theme.trim(), n_notes: nNotes, abc, voice })
      if (!r.exact) await uiConfirm({ title: 'AI 歌词', message: 'AI 字数未精确命中，已按最接近结果填入（建议手动微调）。' })
      store.status = `AI 歌词：${r.lyrics.slice(0, 20)}…`
      return r.lyrics
    } catch (e) {
      await uiConfirm({ title: 'AI 歌词失败', message: e.detail || e.message || String(e) })
      store.status = 'AI 歌词失败'
      return null
    }
  }
  if (mode === 'manual') {
    const t = await uiPrompt({ title: '手动输入歌词', label: `演唱歌词（${nNotes} 个音节，自动逐字切分）：`,
      initial: abcLyrics.value, placeholder: '一闪一闪亮晶晶' })
    if (t === null) return null
    return t.trim()
  }
  return ''
}
/** 对当前工程的人声轨 AI 填词：替换该轨所有歌词（PATCH 每个音符）。 */
async function aiLyrics() {
  const track = store.project.tracks.find(t => t.kind === 'sing' && t.notes?.length)
  if (!track) { await uiConfirm({ title: 'AI 歌词', message: '没有可填词的人声轨。' }); return }
  const theme = await uiPrompt({ title: 'AI 填词', label: `给「${track.name}」（${track.notes.length} 音符）填词，主题：`,
    placeholder: '例如：青春的站台' })
  if (theme === null || !theme.trim()) return
  store.status = `AI 生成歌词中（${track.notes.length} 字，约 10-30s）…`
  try {
    const r = await api('POST', '/ai/lyrics', { theme: theme.trim(), n_notes: track.notes.length })
    if (!r.exact) await uiConfirm({ title: 'AI 歌词', message: '字数未精确命中，已按最接近结果填入（多出的字忽略，缺的字唱「啊」）。' })
    const toks = r.lyrics.replace(/\s+/g, '').split('')
    for (let i = 0; i < track.notes.length; i++) {
      await api('PATCH', `/track/${track.id}/note/${track.notes[i].id}`, { lyric: toks[i] || '啊' })
    }
    store.status = `AI 歌词已填入「${track.name}」：${r.lyrics.slice(0, 24)}…（重新播放生效）`
    await actions.refresh()
  } catch (e) {
    await uiConfirm({ title: 'AI 歌词失败', message: e.detail || e.message || String(e) })
    store.status = 'AI 歌词失败'
  }
}
async function doImportAbc(abc, name) {
  try {
    let voice = null
    // 先探测声部（无 V: 单声部直接导；多声部让用户选）
    const vLines = [...abc.matchAll(/^\s*V\s*:\s*(\S+)/gm)].map(m => m[1])
    if (vLines.length > 1) {
      voice = await uiSelect({ title: '选择演唱声部', message: `检测到 ${vLines.length} 个声部，选择要演唱的：`,
        options: vLines.map(v => ({ value: v, label: v === 'Vocal' ? `歌词演唱（${v}）` : v === 'Ins' ? `伴奏试唱（${v}）· 啊啊啊` : v })) })
      if (voice === null) return
    }
    store.status = 'ABC 解析中…'
    // 探测音符数（AI 填词需要精确字数），随即删除探测轨
    const probe = await api('POST', '/import/abc', { abc, voice, full_band: false, track_name: '__probe__' })
    await api('DELETE', '/track/' + probe.track_id)
    const lyrics = await askLyrics(abc, voice, probe.notes)
    if (lyrics === null) { store.status = '已取消'; return }
    const r = await api('POST', '/import/abc', {
      abc, voice, lyrics: lyrics || null,
      track_name: (name || 'ABC 主唱').replace(/\.(abc|txt)$/i, ''),
    })
    store.status = `ABC 导入完成：${r.notes} 音符 · ${r.bpm} BPM · ${r.beats}/${r.beat_type} · 含 ${r.extra_tracks} 伴奏轨` +
      (r.missing_lyric ? ` · ${r.missing_lyric} 音符无词（唱「啊」）` : '')
    await actions.refresh()
  } catch (e) {
    store.status = 'ABC 导入失败: ' + (e.message || e)
    await uiConfirm({ title: 'ABC 导入失败', message: e.detail || e.message || String(e) })
  }
}
</script>

<template>
  <div class="bar">
    <span class="logo">SVS Studio</span>
    <button @click="newProject">新建</button>
    <button @click="openProject">打开</button>
    <button @click="saveProject">保存</button>
    <span class="sep" />
    <button class="play" @click="togglePlay" :disabled="store.transport.output === 'device' && loadingDevices">{{ store.transport.state === 'buffering' ? 'Ⅱ 缓冲中（取消）' : store.transport.state === 'playing' ? '■ 停止' : '▶ 播放' }}</button>
    <select v-model="store.transport.output" :disabled="store.transport.playing" aria-label="试听输出" title="原生声卡在server本机发声，仍有分块与网络缓冲。">
      <option value="browser">浏览器输出</option>
      <option value="device">原生声卡输出</option>
    </select>
    <template v-if="store.transport.output === 'device'">
      <select v-model="store.transport.output_mode" :disabled="store.transport.playing" aria-label="声卡访问模式" title="独占模式可能拒绝其他应用使用设备；不支持时明确报错，不回退共享。">
        <option value="shared">WASAPI 共享</option>
        <option value="exclusive">WASAPI 独占</option>
      </select>
      <select :value="store.transport.output_device_id" @change="chooseOutputDevice" :disabled="loadingDevices || store.transport.state === 'buffering'" aria-label="原生输出设备" class="output-device" title="播放中可切换同采样率设备；失败不会回退默认设备。">
        <option value="">系统默认设备</option>
        <option v-if="store.transport.output_device_id && !outputDevices.some(d => d.id === store.transport.output_device_id)" :value="store.transport.output_device_id" disabled>所选设备已不可用</option>
        <option v-for="device in outputDevices" :key="device.id + device.name" :value="device.id" :disabled="!device.selectable">{{ device.name }} · {{ device.sample_rate || '?' }}Hz{{ device.selectable ? '' : ' · 不支持' }}</option>
      </select>
      <button @click="refreshOutputDevices" :disabled="loadingDevices" title="刷新本机输出设备">{{ loadingDevices ? '读取中' : '刷新设备' }}</button>
      <span v-if="devicesError" class="play-error" role="alert">{{ devicesError }}</span>
    </template>
    <label class="buffer-setting" title="原生VST及声卡路线的处理分块；越小请求越频繁，更容易欠载。浏览器无VST时不使用此设置。">分块
      <select v-model.number="store.audition.block_frames" :disabled="store.transport.playing" aria-label="试听分块帧数">
        <option :value="512">512帧</option><option :value="1024">1024帧</option><option :value="2048">2048帧</option><option :value="4096">4096帧</option>
      </select>
    </label>
    <label class="buffer-setting" title="启动和欠载恢复所需的处理块数；不是设备驱动缓冲或端到端延迟。">预缓冲
      <select v-model.number="store.audition.target_blocks" :disabled="store.transport.playing" aria-label="试听预缓冲块数">
        <option :value="2">2块</option><option :value="4">4块</option>
      </select>
    </label>
    <span v-if="store.audition.block_frames <= 1024" class="buffer-warning" role="status">小分块可能欠载；不稳时增大分块／预缓冲</span>
    <label class="master" title="主音量">主 <input type="range" min="0" max="1" step="0.01" :value="store.project.master_volume ?? 1" @input="setMasterVolume" @change="saveMasterVolume" /></label>
    <span v-if="store.transport.pending" class="pending">待合成 {{ store.transport.pending }}</span>
    <span v-if="store.transport.state === 'error'" class="play-error" :title="store.transport.error">试唱错误</span>
    <span v-if="store.transport.native" class="native-status" :title="`持续隔离混音会话；处理 ${store.transport.native.processed_blocks || 0} 块，最近处理 ${Number(store.transport.native.processing_ms || 0).toFixed(1)} 毫秒。插件补偿为固定会话预算，旁路保留延迟；缓冲与补偿不是声卡端到端延迟。`">VST 缓冲 {{ Number(store.transport.native.latency_ms || 0).toFixed(0) }}ms · 补偿 {{ Number(store.transport.native.plugin_latency_ms || 0).toFixed(1) }}ms · 欠载 {{ store.transport.native.underruns || 0 }}</span>
    <span v-if="store.transport.native?.output === 'device'" class="native-status" :title="store.transport.native.device">声卡队列 {{ Number(store.transport.native.device_queue_ms || 0).toFixed(0) }}ms · 声卡欠载 {{ store.transport.native.device_underruns || 0 }}</span>
    <label class="bpm">BPM <input v-model.number="bpm" type="number" min="20" max="300" step="1" disabled /></label>
    <span class="sep" />
    <button @click="actions.synth()" :disabled="store.engine.synth_state === 'rendering'"> ⟳ 合成</button>
    <button @click="importAbc" title="导入 ABC 曲谱生成演唱轨">导入 ABC</button>
    <button @click="aiLyrics" title="按主题给当前人声轨 AI 填词">AI 歌词</button>
    <button @click="renderWav">导出 WAV</button>
    <span class="conn" :class="{ ok: connected }">{{ connected ? '● server' : '○ server 未连接' }}</span>
  </div>
</template>

<style scoped>
.bar { min-height: 44px; flex-wrap: wrap; display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  background: #1b1e24; border-bottom: 1px solid #2a2e35; }
.logo { font-weight: 600; margin-right: 10px; color: #7cb2ff; }
button { background: #262b33; color: #d8dce2; border: 1px solid #343a44; border-radius: 4px;
  padding: 5px 12px; cursor: pointer; font-size: 13px; }
button:hover:not(:disabled) { background: #313742; }
button:disabled { opacity: 0.5; cursor: default; }
select { background: #14161a; color: #d8dce2; border: 1px solid #343a44; border-radius: 4px; padding: 5px; max-width: 128px; }
.output-device { max-width: 260px; }
.buffer-setting { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #9aa3ad; white-space: nowrap; }
.buffer-warning { font-size: 11px; color: #e4b867; }
.play { min-width: 84px; }
.sep { width: 1px; height: 22px; background: #2a2e35; margin: 0 6px; }
.bpm { font-size: 12px; color: #9aa3ad; }
.bpm input { width: 54px; background: #14161a; color: #d8dce2; border: 1px solid #343a44; border-radius: 3px; padding: 3px 6px; }
.master, .pending, .play-error { font-size: 11px; color: #9aa3ad; white-space: nowrap; }
.master input { width: 72px; vertical-align: middle; accent-color: #7cb2ff; }
.play-error { color: #f87171; }
.native-status{font-size:11px;color:#b8a9e8;white-space:nowrap;font-variant-numeric:tabular-nums}
.conn { margin-left: auto; font-size: 12px; color: #6b7280; }
.conn.ok { color: #4ade80; }
</style>
