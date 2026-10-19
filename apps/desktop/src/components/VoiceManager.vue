<script setup>
import { onMounted, ref } from 'vue'
import { api, store, actions } from '../stores/project.js'
import { apiUrl } from '../audio/origin.js'

const props = defineProps({
  endpoint: { type: String, default: '/api/inference/voices' },
})
const voices = ref([])
const directory = ref('')
const name = ref('')
const busy = ref(false)
const error = ref('')
const inspected = ref(null)
const trackId = ref('')
const selectedVoice = ref('')
const singingTracks = () => store.project.tracks.filter(track => track.kind === 'sing')
async function assignVoice() {
  if (!trackId.value || !selectedVoice.value) return
  busy.value = true
  try {
    await api('PATCH', `/track/${encodeURIComponent(trackId.value)}`, { singer_id: selectedVoice.value })
    await actions.refresh()
    error.value = ''
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}

async function call(path = '', options = {}) {
  const response = await fetch(apiUrl(`${props.endpoint}${path}`), {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || data.error || response.statusText)
  return data
}
async function refresh() {
  try {
    const response = await Promise.all([call(), fetch(apiUrl('/api/inference/hardware')).then(r => r.ok ? r.json() : null).catch(() => null)])
    voices.value = response[0].voices || []
    runtime.value = response[1]
    error.value = ''
  } catch (e) { error.value = e.message }
}
async function inspect() {
  busy.value = true
  try {
    inspected.value = await call('/inspect', { method: 'POST', body: JSON.stringify({ path: directory.value, name: name.value || undefined }) })
    error.value = ''
  } catch (e) { error.value = e.message; inspected.value = null }
  finally { busy.value = false }
}
async function register() {
  busy.value = true
  try {
    await call('', { method: 'POST', body: JSON.stringify({ path: directory.value, name: name.value || undefined }) })
    inspected.value = null
    directory.value = ''
    name.value = ''
    await refresh()
  } catch (e) { error.value = e.message }
  finally { busy.value = false }
}
async function remove(voice) {
  if (!window.confirm(`从列表移除「${voice.name}」？不会删除声库文件。`)) return
  busy.value = true
  try { await call(`/${encodeURIComponent(voice.id)}`, { method: 'DELETE' }); await refresh() }
  catch (e) { error.value = e.message }
  finally { busy.value = false }
}
onMounted(refresh)
</script>

<template>
  <section class="voice-manager">
    <header><h2>声库管理</h2><button :disabled="busy" @click="refresh">刷新</button></header>
    <article v-if="runtime?.status === 'loaded'" class="runtime"><strong>当前推理会话</strong><span>设备：{{ runtime.provider || '未知' }}</span><span>已加载提供程序：{{ runtime.selected_providers?.join(', ') || '暂无' }}</span><p>已读回 {{ runtime.sessions?.length || 0 }} 个已加载会话；未加载模型不计入可用状态。</p></article>
    <form @submit.prevent="inspect">
      <label>声库目录 <input v-model.trim="directory" required placeholder="声库文件夹的本机绝对路径" /></label>
      <label>显示名称 <input v-model.trim="name" placeholder="可选" /></label>
      <button :disabled="busy || !directory">扫描检查</button>
    </form>
    <p v-if="error" class="error">{{ error }}</p>
    <article v-if="inspected" class="inspection">
      <h3>{{ inspected.name }} <small>{{ inspected.family }}</small></h3>
      <p>模型文件：{{ inspected.model_files?.length || 0 }} · 说话人：{{ inspected.speakers?.length || 0 }} · 授权：{{ inspected.license_status }}</p>
      <p>能力：{{ inspected.capabilities?.join('、') || '未能从元数据确认' }}</p>
      <ul><li v-for="(check, key) in inspected.prerequisites" :key="key">{{ key }}：{{ typeof check === 'object' ? (check.available ? '可用' : check.reason) : (check ? '满足' : '未满足') }}</li></ul>
      <p class="caution">扫描结果不等于推理可用；当前尚未验证该模型的实际合成。</p>
      <button :disabled="busy" @click="register">仅登记此目录</button>
    </article>
    <div v-if="voices.length" class="voices">
      <form class="assignment" @submit.prevent="assignVoice">
        <label>歌声轨 <select v-model="trackId" required><option value="" disabled>选择轨道</option><option v-for="track in singingTracks()" :key="track.id" :value="track.id">{{ track.name }}</option></select></label>
        <label>声库 <select v-model="selectedVoice" required><option value="" disabled>选择声库</option><option value="diffsinger.qixuan">绮萱（内置 DiffSinger）</option><option v-for="voice in voices" :key="voice.id" :value="voice.id">{{ voice.name }}{{ voice.inference_ready ? '' : '（未验证）' }}</option></select></label>
        <button :disabled="busy || !trackId || !selectedVoice">应用到轨道</button>
      </form>
      <article v-for="voice in voices" :key="voice.id" class="voice">
        <div><strong>{{ voice.name }}</strong><small>{{ voice.family }} · {{ voice.id }}</small></div>
        <span :class="voice.inference_ready ? 'ready' : 'unready'">{{ voice.inference_ready ? '可推理' : '未验证/不可用' }}</span>
        <button :disabled="busy" @click="remove(voice)">移除</button>
        <details><summary>元数据与前置条件</summary><pre>{{ JSON.stringify({ speakers: voice.speakers, capabilities: voice.capabilities, licenses: voice.licenses, prerequisites: voice.prerequisites }, null, 2) }}</pre></details>
      </article>
    </div>
    <p v-else class="empty">尚未登记声库。</p>
  </section>
</template>

<style scoped>
.voice-manager { display: grid; gap: 12px; padding: 16px; color: #d8dde5; background: #181b20; font: 13px sans-serif; }
form.assignment { padding: 10px; background: #20242b; }
select { min-width: 200px; padding: 6px; background: #181b20; color: #d8dde5; border: 1px solid #3a414c; }
header, form, .voice { display: flex; align-items: center; gap: 10px; }
header { justify-content: space-between; } h2, h3 { margin: 0; } form { flex-wrap: wrap; }
label { display: grid; gap: 4px; color: #9aa3ad; } input { min-width: 260px; padding: 6px; }
button { padding: 5px 9px; color: #d8dde5; background: #262b33; border: 1px solid #3a414c; border-radius: 4px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }.inspection, .voice { padding: 12px; background: #20242b; border: 1px solid #303741; border-radius: 5px; }
.runtime{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;background:#20242b;border:1px solid #303741;border-radius:5px}.runtime p{margin:0;color:#9aa3ad}
.voices { display: grid; gap: 8px; }.voice { display: grid; grid-template-columns: minmax(180px, 1fr) auto auto; }.voice small { display: block; margin-top: 4px; color: #87919e; }.voice details { grid-column: 1 / -1; }
pre { max-height: 240px; overflow: auto; white-space: pre-wrap; color: #aab2bd; }.ready { color: #8de0ac; }.unready, .empty { color: #f0b45b; }
</style>
