<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { api, store, actions } from '../stores/project.js'
import { apiUrl } from '../audio/origin.js'

const props = defineProps({
  endpoint: { type: String, default: '/api/inference/voices' },
})
const voices = ref([])
const runtime = ref(null)
const directory = ref('')
const name = ref('')
const busy = ref(false)
const error = ref('')
const inspected = ref(null)
const catalog = ref(null)
const installJob = ref(null)
const acceptedTerms = ref(false)
const distribution = ref({ endpoint: '', username: '', password: '', configured: false, packages: [] })
const distributionTermsViewed = ref({})
const distributionJob = ref(null)
const distributionBusy = ref(false)
const distributionTerms = ref({})
const distributionLoading = ref(true)
let distributionInstallTimer = null
const distributionTermsContent = ref({})
const distributionTermsUrls = new Set()
let distributionRevision = 0
function distributionFingerprint(item) {
  return `${distribution.value.endpoint}\n${item.id}\n${item.sha256}\n${item.terms_sha256}`
}
function clearDistributionReview() {
  distributionRevision += 1
  for (const url of distributionTermsUrls) URL.revokeObjectURL(url)
  distributionTermsUrls.clear()
  distributionTerms.value = {}
  distributionTermsViewed.value = {}
  distributionTermsContent.value = {}
}
async function reviewDistributionTerms(item) {
  const revision = distributionRevision
  const fingerprint = distributionFingerprint(item)
  try {
    const result = await call(`/distribution/terms/${encodeURIComponent(item.id)}`)
    if (disposed || revision !== distributionRevision || distributionFingerprint(item) !== fingerprint) return
    if (result.sha256 !== item.terms_sha256) throw new Error('许可材料已变更，请刷新目录后重新阅读。')
    distributionTerms.value[item.id] = false
    distributionTermsViewed.value[item.id] = false
    const previous = distributionTermsContent.value[item.id]?.url
    if (previous) { URL.revokeObjectURL(previous); distributionTermsUrls.delete(previous) }
    const binary = atob(result.content)
    const bytes = Uint8Array.from(binary, character => character.charCodeAt(0))
    const blob = new Blob([bytes], { type: result.content_type || 'application/octet-stream' })
    const url = URL.createObjectURL(blob)
    distributionTermsUrls.add(url)
    if ((result.content_type || '').toLowerCase().startsWith('text/plain')) {
      const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
      distributionTermsContent.value[item.id] = { kind: 'text', text, url, fingerprint }
    } else if ((result.content_type || '').toLowerCase() === 'application/pdf') {
      distributionTermsContent.value[item.id] = { kind: 'pdf', text: '', url, fingerprint }
    } else {
      URL.revokeObjectURL(url)
      distributionTermsUrls.delete(url)
      throw new Error('此许可材料格式不支持内嵌预览；请管理员提供纯文本或 PDF 文件。')
    }
    if (distributionTermsContent.value[item.id].kind === 'text') distributionTermsViewed.value[item.id] = fingerprint
  } catch (e) { error.value = `无法读取原始许可材料：${e.message}` }
}
const installBusy = ref(false)
let installPollTimer = null
let disposed = false
let installGeneration = 0
const activeInstallStates = ['downloading', 'extracting', 'registering']
const trackId = ref('')
const selectedVoice = ref('')
const vocoderForms = ref({})
const vocoderBusy = ref({})
const vocoderErrors = ref({})
const vocoderNotices = ref({})
function vocoderForm(voice) {
  if (!vocoderForms.value[voice.id]) {
    vocoderForms.value[voice.id] = { path: '', acknowledged: false }
  }
  return vocoderForms.value[voice.id]
}
async function updateVocoder(voice, clear = false) {
  const form = vocoderForm(voice)
  if (!clear && (!form.path.trim() || !form.acknowledged)) return
  vocoderBusy.value[voice.id] = true
  vocoderErrors.value[voice.id] = ''
  vocoderNotices.value[voice.id] = ''
  try {
    await actions.stop()
    await call(`/${encodeURIComponent(voice.id)}/vocoder`, {
      method: 'PATCH',
      body: JSON.stringify({ path: clear ? null : form.path.trim(), acknowledged_license: true }),
    })
    form.path = ''
    form.acknowledged = false
    await refresh()
    vocoderNotices.value[voice.id] = '配置已更新。已准备或缓冲的音频仍使用旧设置；请重新合成并重新播放。'
  } catch (e) {
    vocoderErrors.value[voice.id] = e.message
  } finally {
    vocoderBusy.value[voice.id] = false
  }
}
function vocoderSourceLabel(source) {
  return ({ external: '外部绑定', bundled: '随声库提供', missing: '缺失' })[source] || '未知'
}
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
function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return '未知'
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let amount = value / 1024
  let index = 0
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++ }
  return `${amount.toFixed(1)} ${units[index]}`
}
function stopInstallPolling() {
  if (installPollTimer !== null) { clearTimeout(installPollTimer); installPollTimer = null }
}
function scheduleInstallPoll() {
  stopInstallPolling()
  if (!disposed && activeInstallStates.includes(installJob.value?.state)) {
    installPollTimer = setTimeout(pollInstall, 1200)
  }
}
async function loadCatalog() {
  try {
    const data = await call('/catalog')
    catalog.value = data.items?.find(item => item.id === 'qixuan-2.7.0') || null
  } catch (e) { error.value = `无法读取官方声库目录：${e.message}` }
}
async function pollInstall() {
  installPollTimer = null
  if (disposed) return
  const generation = installGeneration
  try {
    const data = await call('/install')
    if (disposed || generation !== installGeneration) return
    installJob.value = data.job
    if (installJob.value?.state === 'completed') await refresh()
  } catch (e) {
    if (disposed || generation !== installGeneration) return
    error.value = `无法读取安装进度：${e.message}`
  }
  scheduleInstallPoll()
}
async function startInstall() {
  if (!acceptedTerms.value || installBusy.value) return
  installBusy.value = true
  installGeneration++
  stopInstallPolling()
  try {
    const data = await call('/install', { method: 'POST', body: JSON.stringify({ catalog_id: 'qixuan-2.7.0', accepted_terms: true }) })
    installJob.value = data.job
    error.value = ''
    scheduleInstallPoll()
  } catch (e) { error.value = `无法开始声库安装：${e.message}` }
  finally { installBusy.value = false }
}
async function loadDistribution() {
  try {
    const state = await call('/distribution/status')
    distribution.value.endpoint = state.endpoint || ''
    distribution.value.username = state.username || ''
    distribution.value.configured = state.configured === true
    distributionJob.value = state.job
    if (state.configured) await refreshDistributionCatalog()
    scheduleDistributionPoll()
  } catch (e) { error.value = `无法读取声库分发配置：${e.message}` }
  finally { distributionLoading.value = false }
}
async function configureDistribution() {
  distributionBusy.value = true
  clearDistributionReview()
  distribution.value.packages = []
  try {
    const result = await call('/distribution/configure', { method: 'POST', body: JSON.stringify({ endpoint: distribution.value.endpoint.trim(), username: distribution.value.username.trim(), password: distribution.value.password }) })
    distribution.value.password = ''
    distribution.value.configured = result.configured
    await refreshDistributionCatalog()
    error.value = ''
  } catch (e) { error.value = `无法配置受信任声库服务：${e.message}` }
  finally { distributionBusy.value = false }
}
async function disconnectDistribution() {
  if (!window.confirm('断开当前受信任声库服务并清除本机保存的服务令牌？')) return
  distributionBusy.value = true
  clearDistributionReview()
  try { await call('/distribution/disconnect', { method: 'POST' }); distribution.value.configured = false; distribution.value.packages = []; distributionJob.value = null }
  catch (e) { error.value = e.message }
  finally { distributionBusy.value = false }
}
async function refreshDistributionCatalog() {
  clearDistributionReview()
  try { distribution.value.packages = (await call('/distribution/catalog')).packages || []; error.value = '' }
  catch (e) { distribution.value.packages = []; error.value = `无法读取审核发布的声库：${e.message}` }
}
async function installDistribution(item) {
  if (!distributionTerms.value[item.id] || distributionTermsViewed.value[item.id] !== distributionFingerprint(item) || distributionBusy.value) return
  distributionBusy.value = true
  try {
    distributionJob.value = await call('/distribution/install', { method: 'POST', body: JSON.stringify({ package_id: item.id, accepted_terms: true,
      accepted_terms_sha256: item.terms_sha256, accepted_archive_sha256: item.sha256 }) })
    scheduleDistributionPoll()
  } catch (e) { error.value = `无法安装分发声库：${e.message}` }
  finally { distributionBusy.value = false }
}
function stopDistributionPolling() {
  if (distributionInstallTimer !== null) { clearTimeout(distributionInstallTimer); distributionInstallTimer = null }
}
function scheduleDistributionPoll() {
  stopDistributionPolling()
  if (!disposed && activeInstallStates.includes(distributionJob.value?.state)) distributionInstallTimer = setTimeout(pollDistributionInstall, 1200)
}
async function pollDistributionInstall() {
  distributionInstallTimer = null
  if (disposed) return
  try {
    distributionJob.value = (await call('/distribution/install')).job
    if (distributionJob.value?.state === 'completed') await refresh()
  } catch (e) { error.value = `无法读取分发安装进度：${e.message}` }
  scheduleDistributionPoll()
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
onMounted(async () => {
  await Promise.all([refresh(), loadCatalog(), loadDistribution()])
  await Promise.all([pollInstall(), pollDistributionInstall()])
})
onUnmounted(() => {
  disposed = true
  stopInstallPolling()
  stopDistributionPolling()
  clearDistributionReview()
})
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
    <article class="catalog-install">
      <h3>官方声库下载</h3>
      <template v-if="catalog">
        <p><strong>{{ catalog.name }}</strong> · {{ catalog.version }} · {{ formatBytes(catalog.bytes) }} · 免费</p>
        <p>SHA-256：<code>{{ catalog.sha256 }}</code></p><p>{{ catalog.notice }}</p>
        <p>来源：<a :href="catalog.source_url" target="_blank" rel="noopener noreferrer">官方来源</a> · <a :href="catalog.terms_url" target="_blank" rel="noopener noreferrer">许可条款</a></p>
        <p class="caution">安装不包含 vocoder。登记声库后，可绑定本机已有且兼容的 vocoder 目录；不会自动下载或复制文件。</p>
        <p>网络中断或退出后保留断点；再次接受条款并点击继续下载即可恢复。服务器不支持续传时安全重下，完成后校验 SHA-256。</p>
        <p v-if="(installJob?.downloaded_bytes ?? catalog.downloaded_bytes) > 0 && installJob?.state !== 'completed'">本地已保存 {{ formatBytes(installJob?.downloaded_bytes ?? catalog.downloaded_bytes) }} / {{ formatBytes(catalog.bytes) }}</p>
        <label class="accept"><input v-model="acceptedTerms" type="checkbox" />我已阅读并接受该声库许可条款</label>
        <button :disabled="!acceptedTerms || installBusy || activeInstallStates.includes(installJob?.state)" @click="startInstall">{{ installBusy ? '正在请求…' : (installJob?.downloaded_bytes ?? catalog.downloaded_bytes) > 0 && installJob?.state !== 'completed' ? '继续下载并安装' : '下载并安装' }}</button>
      </template>
      <p v-else>官方目录暂不可用。</p>
      <div v-if="installJob" class="install-status" aria-live="polite">
        <strong>安装状态：{{ installJob.state }}</strong>
        <p v-if="installJob.state === 'downloading'">已下载 {{ formatBytes(installJob.downloaded_bytes) }}<template v-if="installJob.total_bytes"> / {{ formatBytes(installJob.total_bytes) }}</template></p>
        <p v-if="installJob.resumed_bytes > 0">本次从 {{ formatBytes(installJob.resumed_bytes) }} 断点恢复</p>
        <p v-if="installJob.error" class="error">{{ installJob.error }}</p>
        <p v-if="installJob.state === 'completed'">安装完成：{{ installJob.destination }}。仍需配置运行时依赖并为声库绑定兼容 vocoder；尚未验证合成。</p>
      </div>
    </article>
    <article class="distribution-install">
      <h3>受信任的自建声库服务</h3>
      <p>仅配置你明确管理或信任的服务端点。HTTPS 必需；明文 HTTP 仅允许本机 loopback。安装仅使用审核发布的原始包及完整性校验，不会自动执行模型或授予商业/再分发权利。</p>
      <form @submit.prevent="configureDistribution">
        <label>服务端点<input v-model.trim="distribution.endpoint" required placeholder="https://voice.example 或 http://127.0.0.1:8765" :disabled="distributionLoading || distributionBusy" /></label>
        <label>用户名<input v-model.trim="distribution.username" required autocomplete="username" :disabled="distributionLoading || distributionBusy" /></label>
        <label>密码<input v-model="distribution.password" type="password" required autocomplete="current-password" :disabled="distributionLoading || distributionBusy" /></label>
        <button :disabled="distributionLoading || distributionBusy || !distribution.endpoint || !distribution.username || !distribution.password">{{ distributionLoading ? '读取配置…' : distributionBusy ? '正在连接…' : '验证并信任此端点' }}</button>
      </form>
      <p v-if="distribution.configured" class="caution">已信任：{{ distribution.endpoint }} · {{ distribution.username }}。密码不会保存在前端；令牌仅由本机 DPAPI 保护并与项目同步凭据隔离。</p>
      <button v-if="distribution.configured" :disabled="distributionBusy" @click="refreshDistributionCatalog">刷新已审核目录</button>
      <button v-if="distribution.configured" :disabled="distributionBusy" @click="disconnectDistribution">断开并清除令牌</button>
      <article v-for="item in distribution.packages" :key="item.id" class="distribution-package">
        <strong>{{ item.title }} · {{ item.version }}</strong>
        <p>{{ formatBytes(item.byte_length) }} · SHA-256 <code>{{ item.sha256 }}</code></p>
        <p>权利说明（不构成新授权）：{{ item.rights_statement }}</p>
        <button :disabled="distributionBusy" @click="reviewDistributionTerms(item)">查看原始许可材料（SHA-256 校验）</button>
        <section v-if="distributionTermsContent[item.id]" class="terms-viewer" aria-live="polite">
          <template v-if="distributionTermsContent[item.id].kind === 'text'">
            <strong>已校验原始许可材料（SHA-256 {{ item.terms_sha256 }}）</strong>
            <pre>{{ distributionTermsContent[item.id].text }}</pre>
          </template>
          <template v-else>
            <strong>已校验 PDF 原始许可材料（SHA-256 {{ item.terms_sha256 }}）</strong>
            <a :href="distributionTermsContent[item.id].url" :download="`${item.title}-license.pdf`" @click="distributionTermsViewed[item.id] = distributionTermsContent[item.id].fingerprint">下载并阅读 PDF 许可材料</a>
            <p>请打开并阅读下载的 PDF 后，再勾选下方确认框。</p>
          </template>
        </section>
        <label class="accept"><input v-model="distributionTerms[item.id]" type="checkbox" :disabled="distributionTermsViewed[item.id] !== distributionFingerprint(item)" />我已阅读服务端所附原始许可材料，并自行确认其适用范围；此确认不授予任何额外权利。</label>
        <button :disabled="!distributionTerms[item.id] || distributionTermsViewed[item.id] !== distributionFingerprint(item) || distributionBusy || activeInstallStates.includes(distributionJob?.state)" @click="installDistribution(item)">安装此已审核原包</button>
      </article>
      <div v-if="distributionJob" class="install-status" aria-live="polite">
        <strong>自建分发安装：{{ distributionJob.state }}</strong>
        <p v-if="distributionJob.state === 'downloading'">{{ formatBytes(distributionJob.downloaded_bytes) }} / {{ formatBytes(distributionJob.total_bytes) }}</p>
        <p v-if="distributionJob.error" class="error">{{ distributionJob.error }}</p>
        <p v-if="distributionJob.state === 'completed'">安装完成：{{ distributionJob.destination }}。模型未自动加载或执行。</p>
      </div>
    </article>
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
        <label>声库 <select v-model="selectedVoice" required><option value="" disabled>选择声库</option><option value="diffsinger.qixuan">绮萱（内置 DiffSinger）</option><option v-for="voice in voices" :key="voice.id" :value="voice.id">{{ voice.name }}{{ voice.inference_ready ? '' : '（前置条件未齐）' }}</option></select></label>
        <button :disabled="busy || !trackId || !selectedVoice">应用到轨道</button>
      </form>
      <article v-for="voice in voices" :key="voice.id" class="voice">
        <div><strong>{{ voice.name }}</strong><small>{{ voice.family }} · {{ voice.id }}</small></div>
        <span :class="voice.inference_ready ? 'ready' : 'unready'">{{ voice.inference_ready ? '前置文件齐备（未实测）' : '前置条件未齐（未验证）' }}</span>
        <button :disabled="busy" @click="remove(voice)">移除</button>
        <section class="vocoder-binding" :aria-label="`${voice.name} 的 vocoder 配置`">
          <strong>Vocoder：{{ vocoderSourceLabel(voice.vocoder?.source) }}</strong>
          <small>当前目录：{{ voice.vocoder_path || voice.vocoder?.path || '未配置' }}</small>
          <small>兼容性：{{ voice.vocoder?.compatible === true ? '兼容' : voice.vocoder?.compatible === false ? '不兼容' : '未确认' }}</small>
          <p v-if="voice.vocoder?.compatible === false" class="error">Vocoder 与该声库不兼容。</p>
          <ul v-if="voice.vocoder?.issues?.length || voice.vocoder?.missing_files?.length">
            <li v-for="(issue, index) in voice.vocoder.issues || []" :key="`issue-${index}`" class="error">{{ issue }}</li>
            <li v-for="file in voice.vocoder.missing_files || []" :key="file" class="unready">缺少文件：{{ file }}</li>
          </ul>
          <label>本机已有的 vocoder 目录 <input v-model.trim="vocoderForm(voice).path" placeholder="包含 vocoder.yaml 的本机绝对路径" /></label>
          <label class="accept"><input v-model="vocoderForm(voice).acknowledged" type="checkbox" />我已独立阅读该 vocoder 的许可；此确认不授予商业使用权。</label>
          <div class="vocoder-actions">
            <button :disabled="vocoderBusy[voice.id] || !vocoderForm(voice).path || !vocoderForm(voice).acknowledged" @click="updateVocoder(voice)">{{ vocoderBusy[voice.id] ? '正在绑定…' : '绑定目录' }}</button>
            <button :disabled="vocoderBusy[voice.id] || !voice.vocoder_path" @click="updateVocoder(voice, true)">清除绑定</button>
          </div>
          <p v-if="vocoderErrors[voice.id]" class="error" role="alert">Vocoder 配置失败：{{ vocoderErrors[voice.id] }}</p>
          <p v-if="vocoderNotices[voice.id]" class="ready" role="status">{{ vocoderNotices[voice.id] }}</p>
          <p>运行时需在应用启动时配置 SVS_GPU_PYTHON 和 SVS_ABC2VOCALS_DIR；修改环境配置后需重启应用。</p>
        </section>
        <details><summary>元数据与前置条件</summary><pre>{{ JSON.stringify({ speakers: voice.speakers, capabilities: voice.capabilities, licenses: voice.licenses, prerequisites: voice.prerequisites }, null, 2) }}</pre></details>
      </article>
    </div>
    <p v-else-if="installJob?.state === 'completed'" class="empty">声库已安装；登记状态正在刷新。</p>
    <p v-else class="empty">尚未登记声库。</p>
  </section>
</template>

<style scoped>
.voice-manager { display: grid; gap: 12px; padding: 16px; color: #d8dde5; background: #181b20; font: 13px sans-serif; }
form.assignment { padding: 10px; background: #20242b; }
select { min-width: 200px; padding: 6px; background: #181b20; color: #d8dde5; border: 1px solid #3a414c; }
header, form, .voice { display: flex; align-items: center; gap: 10px; }
header { justify-content: space-between; } h2, h3 { margin: 0; } form { flex-wrap: wrap; }
label { display: grid; gap: 4px; min-width: 0; color: #9aa3ad; } input { box-sizing: border-box; width: 100%; min-width: 0; padding: 6px 9px; background: #171c24; color: #d8dde5; border: 1px solid #3a414c; border-radius: 4px; font: inherit; } .voice-manager>form>label { width: min(260px,100%); } input:focus-visible { outline: 2px solid #8b70e8; outline-offset: 2px; }
button { padding: 5px 9px; color: #d8dde5; background: #262b33; border: 1px solid #3a414c; border-radius: 4px; cursor: pointer; }
.voice-manager form{align-items:flex-end}.voice-manager form input,.voice-manager form select,.voice-manager form button{box-sizing:border-box;height:32px}
button:disabled { opacity: .5; cursor: default; }.inspection, .voice { padding: 12px; background: #20242b; border: 1px solid #303741; border-radius: 5px; }
.runtime{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;background:#20242b;border:1px solid #303741;border-radius:5px}.runtime p{margin:0;color:#9aa3ad}
.voices { display: grid; gap: 8px; }.voice { display: grid; grid-template-columns: minmax(180px, 1fr) auto auto; }.voice small { display: block; margin-top: 4px; color: #87919e; }.voice details { grid-column: 1 / -1; }
.catalog-install { display: grid; gap: 8px; min-width: 0; padding: 12px; background: #20242b; border: 1px solid #303741; border-radius: 5px; }
.catalog-install p, .install-status p { margin: 0; overflow-wrap: anywhere; }
.catalog-install code { overflow-wrap: anywhere; }
.catalog-install a { color: #b8a5ff; }
.accept { display: flex; align-items: center; gap: 8px; }
.accept input { width: auto; }
.install-status { display: grid; gap: 6px; padding-top: 8px; }
.vocoder-binding { grid-column: 1 / -1; display: grid; gap: 6px; min-width: 0; padding-top: 8px; color: #9aa3ad; }
.vocoder-binding small, .vocoder-binding p, .vocoder-binding ul { margin: 0; overflow-wrap: anywhere; }
.vocoder-binding ul { padding-left: 20px; }
.vocoder-binding label { width: 100%; }
.vocoder-binding input:not([type="checkbox"]) { height: 32px; }
.vocoder-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.vocoder-binding .accept { display: flex; align-items: center; gap: 8px; }
.vocoder-binding .accept input { width: auto; }
.error { color: #ff9292; overflow-wrap: anywhere; }
@media (max-width: 480px) {
  .voice-manager { padding: 12px; }
  .voice-manager form > label { width: 100%; }
  .catalog-install button { width: 100%; min-height: 40px; }
  .voice { grid-template-columns: minmax(0, 1fr); }
  .voice-manager select { min-width: 0; width: 100%; }
}
pre { max-height: 240px; overflow: auto; white-space: pre-wrap; color: #aab2bd; }.ready { color: #8de0ac; }.unready, .empty { color: #f0b45b; }
</style>
