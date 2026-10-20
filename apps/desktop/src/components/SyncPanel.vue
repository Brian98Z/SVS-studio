<script setup>
import { computed, onMounted, ref } from 'vue'
import { apiUrl } from '../audio/origin.js'
import { actions } from '../stores/project.js'

const url = ref('')
const username = ref('')
const password = ref('')
const status = ref(null)
const items = ref([])
const remoteName = ref('')
const selectedName = ref('')
const destination = ref('')
const projectBase = ref('')
const restoreConfirmed = ref(false)
const busy = ref(false)
const message = ref('')
const error = ref('')
const configured = computed(() => Boolean(status.value?.configured))
const selectedItem = computed(() => items.value.find(item => item.name === selectedName.value))
const validRemoteName = computed(() => /^([^\\/:*?"<>|]+)\.svsbundle$/i.test(remoteName.value.trim()) && remoteName.value.trim() !== '.' && remoteName.value.trim() !== '..')

async function request(path, body) {
  const response = await fetch(apiUrl(path), {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
  const result = await response.json().catch(() => ({}))
  if (!response.ok) {
    const cause = new Error(result.detail || result.error || response.statusText)
    Object.assign(cause, result)
    throw cause
  }
  return result
}
async function run(work) {
  if (busy.value) return
  busy.value = true
  error.value = ''; message.value = ''
  try { await work() } catch (cause) { error.value = cause.message || String(cause) }
  finally { busy.value = false }
}
async function refreshStatus() {
  const result = await request('/api/inference/sync', { action: 'status' })
  status.value = result
  if (result.url) url.value = result.url
  if (result.username) username.value = result.username
  if (result.configured) await refreshList()
  else { items.value = []; selectedName.value = '' }
}
async function refreshList() {
  const result = await request('/api/inference/sync', { action: 'list' })
  items.value = result.items || []
  if (!items.value.some(item => item.name === selectedName.value)) selectedName.value = ''
}
function loadStatus() { run(refreshStatus) }
function loadList() { run(refreshList) }
function configure() {
  run(async () => {
    const result = await request('/api/inference/sync', { action: 'configure', url: url.value.trim(), username: username.value.trim(), password: password.value })
    status.value = result
    password.value = ''
    message.value = 'WebDAV 连接配置已保存。凭据仅保存在本机安全存储中。'
    await refreshList()
  })
}
function disconnect() {
  run(async () => {
    const result = await request('/api/inference/sync', { action: 'disconnect' })
    status.value = result
    items.value = []; selectedName.value = ''
    password.value = ''
    message.value = '已断开 WebDAV 并删除本机保存的配置与凭据。'
  })
}
function pushProject() {
  run(async () => {
    if (!validRemoteName.value) throw new Error('请输入不含目录路径、以 .svsbundle 结尾的远程文件名。')
    const result = await request('/api/interchange', {
      action: 'sync_push', project_base: projectBase.value.trim() || undefined,
      remote_name: remoteName.value.trim(),
    })
    if (result.status === 'conflict') {
      message.value = `远程已有内容未被覆盖；本次上传保存为冲突副本「${result.conflict_remote_name || result.remote_name}」。原文件保持不变。`
      await refreshList()
      selectedName.value = result.conflict_remote_name || result.remote_name || ''
    } else {
      message.value = `完整工程包已上传：${result.remote_name || remoteName.value.trim()}。`
      await refreshList()
      selectedName.value = result.remote_name || remoteName.value.trim()
    }
  })
}
function pullProject() {
  run(async () => {
    if (!selectedItem.value) throw new Error('请先选择要恢复的远程工程包。')
    if (!destination.value.trim()) throw new Error('请填写一个尚不存在的本机恢复目录。')
    if (!restoreConfirmed.value) throw new Error('请先确认已了解恢复会替换当前工程。')
    try {
      const result = await request('/api/interchange', {
        action: 'sync_pull', project_base: projectBase.value.trim() || undefined,
        remote_name: selectedName.value, destination: destination.value.trim(),
      })
      await actions.refresh()
      restoreConfirmed.value = false
      message.value = `已恢复工程「${result.remote_name || selectedName.value}」。当前工程及媒体备份：${result.backup_path || '后端未返回备份路径'}；新工程位置：${result.project_path || result.path || destination.value.trim()}。`
    } catch (cause) {
      if (cause.project_path || cause.backup_path) {
        message.value = `操作未完成。后端报告的恢复位置：${cause.project_path || '未提供'}；备份位置：${cause.backup_path || '未提供'}。`
      }
      throw cause
    }
  })
}
onMounted(() => { loadStatus() })
</script>

<template>
  <section class="sync-panel">
    <header class="sync-heading">
      <div><h2>手动 WebDAV 工程同步</h2><p>仅在你明确上传或恢复时传输完整 .svsbundle；不会实时同步。</p></div>
      <span class="state" :class="{ connected: configured }" role="status">{{ configured ? '已配置' : '未配置' }}</span>
    </header>
    <p class="scope-note">同步内容包含工程与引用的外部音频；不包含声库、模型或插件。</p>

    <section class="sync-section" aria-labelledby="sync-connection-title">
      <h3 id="sync-connection-title">WebDAV 连接</h3>
      <label for="sync-url">WebDAV 集合 URL</label>
      <input id="sync-url" v-model="url" type="url" autocomplete="url" placeholder="https://dav.example.com/remote.php/dav/files/user/music/" :disabled="busy" />
      <div class="field-grid">
        <label for="sync-username">用户名<input id="sync-username" v-model="username" autocomplete="username" :disabled="busy" /></label>
        <label for="sync-password">应用密码<input id="sync-password" v-model="password" type="password" autocomplete="new-password" :disabled="busy" placeholder="WebDAV 应用密码" /></label>
      </div>
      <p class="help">请使用服务端提供的应用密码。密码不会写入浏览器本地存储或工程包。</p>
      <div class="button-row">
        <button class="primary" :disabled="busy || !url.trim() || !username.trim() || !password" @click="configure">{{ busy ? '处理中…' : configured ? '保存并重新连接' : '连接并保存' }}</button>
        <button :disabled="busy" @click="loadStatus">刷新状态</button>
        <button class="danger" :disabled="busy || !configured" @click="disconnect">断开并删除凭据</button>
      </div>
      <p v-if="status" class="help">凭据状态：{{ status.credential_stored ? '已安全保存' : configured ? '未确认' : '未保存' }}<span v-if="status.url"> · 集合：{{ status.url }}</span></p>
    </section>

    <section class="sync-section" aria-labelledby="sync-upload-title">
      <h3 id="sync-upload-title">上传当前工程</h3>
      <label for="sync-project-base">工程原目录（相对媒体路径的基准目录，可选）</label>
      <input id="sync-project-base" v-model="projectBase" placeholder="例如 C:/Music/MyProject" :disabled="busy || !configured" />
      <label for="sync-remote-name">远程文件名</label>
      <input id="sync-remote-name" v-model="remoteName" class="mono" :disabled="busy || !configured" placeholder="MyProject.svsbundle" aria-describedby="sync-name-help" />
      <p id="sync-name-help" class="help">只允许集合目录中的 .svsbundle 文件名，不接受路径。已存在的远程文件不会被盲目覆盖；冲突时会保留原文件并创建冲突副本。</p>
      <button class="primary" :disabled="busy || !configured || !validRemoteName" @click="pushProject">{{ busy ? '处理中…' : '上传完整工程包' }}</button>
    </section>

    <section class="sync-section" aria-labelledby="sync-restore-title">
      <div class="section-heading"><div><h3 id="sync-restore-title">从远程恢复</h3><p>先刷新并明确选择远程工程包，再恢复到一个新目录。</p></div><button :disabled="busy || !configured" @click="loadList">刷新列表</button></div>
      <p v-if="configured && !items.length" class="empty">集合目录中没有 .svsbundle 工程包。</p>
      <ul v-else-if="items.length" class="remote-list" aria-label="远程工程包列表">
        <li v-for="item in items" :key="item.name">
          <label class="remote-choice"><input v-model="selectedName" type="radio" name="sync-remote-item" :value="item.name" :disabled="busy" /><span class="remote-info"><strong>{{ item.name }}</strong><small>{{ item.size }} 字节<span v-if="item.modified"> · {{ item.modified }}</span><span v-if="item.etag"> · ETag {{ item.etag }}</span></small></span></label>
        </li>
      </ul>
      <label for="sync-destination">恢复到本机新目录（必须尚不存在）</label>
      <input id="sync-destination" v-model="destination" :disabled="busy || !configured" placeholder="例如 C:/Music/RestoredProject" />
      <p class="warning">恢复会替换当前打开的工程；操作前会在本机保留当前工程及媒体的备份。请确认恢复目录不存在，并且不要关闭应用。</p>
      <label class="restore-confirm"><input v-model="restoreConfirmed" type="checkbox" :disabled="busy" />我确认替换当前工程，并保留操作前备份。</label>
      <button class="danger-action" :disabled="busy || !configured || !selectedName || !destination.trim() || !restoreConfirmed" @click="pullProject">{{ busy ? '处理中…' : '确认恢复所选工程（替换当前工程）' }}</button>
    </section>

    <p v-if="message" class="feedback success" role="status">{{ message }}</p>
    <p v-if="error" class="feedback error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.sync-panel{display:grid;gap:14px;width:min(760px,100%);margin:0 auto;color:#dce4f2;font:13px/1.5 system-ui,sans-serif;min-width:0}.sync-heading,.section-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.sync-heading h2{margin:0;font-size:18px}.sync-heading p,.section-heading p{margin:4px 0 0;color:#929baa}.state{flex:0 0 auto;padding:3px 8px;border:1px solid #48515e;border-radius:999px;color:#aab2bf;font-size:12px}.state.connected{border-color:#397653;color:#86d6a2}.scope-note,.help,.empty{margin:0;color:#929baa;font-size:12px}.scope-note{padding:9px 11px;border-left:2px solid #7189a9;background:#1b222c}.sync-section{display:grid;gap:9px;padding:14px;background:#1c2027;border:1px solid #303742;border-radius:8px;min-width:0}.sync-section h3{margin:0;font-size:14px}.sync-section>label,.field-grid label{display:grid;gap:5px;color:#c2c9d3;font-weight:550}.sync-section input:not([type=radio]){box-sizing:border-box;width:100%;min-width:0;min-height:36px;background:#12161d;border:1px solid #3a4351;border-radius:5px;padding:7px 10px;color:#e5e7eb;font:inherit;font-weight:400}.sync-section input:focus{outline:2px solid #637c9d;outline-offset:1px}.field-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace!important}.button-row{display:flex;flex-wrap:wrap;gap:8px}.sync-panel button{min-height:36px;border:1px solid #414b5b;border-radius:5px;background:#2a3240;color:inherit;padding:7px 12px;font:inherit;cursor:pointer}.sync-panel button:hover:not(:disabled){background:#354157}.sync-panel button:disabled{opacity:.45;cursor:default}.sync-panel button.primary{background:#d8dce3;color:#171a20;border-color:transparent;font-weight:650}.sync-panel button.primary:hover:not(:disabled){background:#fff}.sync-panel button.danger{color:#ffaaa2}.sync-panel button.danger-action{background:#512c2a;border-color:#74413d;color:#ffd0ca;font-weight:600}.sync-panel button.danger-action:hover:not(:disabled){background:#663633}.remote-list{list-style:none;padding:0;margin:0;display:grid;gap:6px;max-height:220px;overflow:auto}.remote-list li{min-width:0}.remote-choice{display:flex;align-items:center;gap:9px;padding:9px;border:1px solid #333b47;border-radius:6px;cursor:pointer}.remote-choice:has(input:checked){border-color:#7189a9;background:#232c38}.remote-choice input{accent-color:#91a9c7}.remote-info{display:grid;gap:2px;min-width:0}.remote-info strong{overflow-wrap:anywhere}.remote-info small{color:#929baa;overflow-wrap:anywhere}.warning{margin:0;padding:9px 10px;border-radius:5px;background:#332c20;color:#e7c77f;font-size:12px}.feedback{padding:9px 11px;border-radius:6px;overflow-wrap:anywhere}.success{color:#86d6a2;background:#183126}.error{color:#ff9388;background:#392321}@media(max-width:600px){.field-grid{grid-template-columns:minmax(0,1fr)}.sync-section{padding:12px}.section-heading{align-items:center}.button-row button{flex:1 1 auto}}
.restore-confirm{display:flex!important;align-items:flex-start;gap:8px!important}.restore-confirm input[type=checkbox]{width:16px;min-height:16px;flex:0 0 16px;margin-top:2px;accent-color:#91a9c7}
</style>
