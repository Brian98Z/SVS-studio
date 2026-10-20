<script setup>
import { computed, onMounted, onUnmounted, ref, toRaw } from 'vue'
import EffectParameterGrid from './EffectParameterGrid.vue'
import { defaultMixerConfig } from '../audio/fx.js'
import { apiUrl } from '../audio/origin.js'
import { effectLabel } from '../audio/effect-labels.js'
import { actions, api, store } from '../stores/project.js'

const props = defineProps({ modelValue: { type: Object, default: defaultMixerConfig }, tracks: { type: Array, default: () => [] } })
const emit = defineEmits(['update:modelValue', 'track-update'])
const selectedId = defineModel('selectedId', { default: 'master' })
const ids = computed(() => [{ id: 'master', name: 'Master' }, ...props.tracks.map(t => ({ id: t.id, name: t.name })), ...(props.modelValue?.buses||[]).map(b=>({id:`bus:${b.id}`,name:b.name||b.id}))])
const channel = computed(() => selectedId.value === 'master'
  ? { volume: 1, effects: props.modelValue?.master?.effects || [] }
  : selectedId.value.startsWith('bus:')
    ? props.modelValue?.buses?.find(b=>`bus:${b.id}`===selectedId.value)
    : props.tracks.find(t => t.id === selectedId.value))
const effects = computed(() => selectedId.value.startsWith('bus:') ? channel.value?.effects||[] : selectedId.value === 'master' ? channel.value?.effects||[] : channel.value?.fx||[])
function patchChannel(patch) {
  if (selectedId.value !== 'master') {
    if(selectedId.value.startsWith('bus:')){
      const config=structuredClone(toRaw(props.modelValue||defaultMixerConfig())),id=selectedId.value.slice(4)
      config.buses=(config.buses||[]).map(b=>b.id===id?{...b,...patch}:b);emit('update:modelValue',config)
    } else emit('track-update', { id: selectedId.value, patch })
    return
  }
  const config = structuredClone(toRaw(props.modelValue || defaultMixerConfig()))
  config.master = { ...config.master, effects: patch.effects || config.master?.effects || [] }
  emit('update:modelValue', config)
}
function patchEffect(index, patch) { const key=selectedId.value==='master'||selectedId.value.startsWith('bus:')?'effects':'fx';patchChannel({[key]:effects.value.map((e,i)=>i===index?{...e,...patch}:e)})}
function addEffect(type) { const initial=type==='eq3'?{low_db:0,mid_db:0,high_db:0,low_hz:250,high_hz:4000}:type==='reverb'?{wet:.15,room:.5}:{threshold_db:-18,ratio:4,attack_ms:10,release_ms:100};const key=selectedId.value==='master'||selectedId.value.startsWith('bus:')?'effects':'fx';patchChannel({[key]:[...effects.value,{type,enabled:false,...initial}]}) }
function removeEffect(index){const key=selectedId.value==='master'||selectedId.value.startsWith('bus:')?'effects':'fx';patchChannel({[key]:effects.value.filter((_,i)=>i!==index)})}
function numeric(index,key,event){patchEffect(index,{[key]:Number(event.target.value)})}
const vstMetadata = ref({})
const vstErrors = ref({})
const draftVstPath = ref('plugins/')
const availableVstPlugins = ref([])
const projectEditor = ref(null)
const editorError = ref('')
const editorMessage = ref('')
const editorBusy = ref(false)
let editorTimer = null
let disposed = false
const editorRunning = computed(() => projectEditor.value && !['completed', 'failed'].includes(projectEditor.value.state))
async function editorRequest(body) {
  const endpoint = body.session_id ? '/api/inference/vst/realtime/editor' : '/api/inference/vst/editor'
  const response = await fetch(apiUrl(endpoint), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  const result = await response.json()
  if (!response.ok) throw new Error(result.detail || result.error || response.statusText)
  return result
}
function editorTarget(index) {
  return selectedId.value === 'master' ? { kind: 'master', id: 'master', index }
    : selectedId.value.startsWith('bus:') ? { kind: 'bus', id: selectedId.value.slice(4), index }
      : { kind: 'track', id: selectedId.value, index }
}
async function pollProjectEditor() {
  if (disposed || !projectEditor.value) return
  try {
    const result = await editorRequest({ action: 'status', id: projectEditor.value.id, session_id: projectEditor.value.session_id })
    if (disposed) return
    projectEditor.value = { ...projectEditor.value, ...result }
    if (result.state === 'completed') { editorMessage.value = '参数与插件预设状态已读回，尚未修改工程。确认应用后须重新播放；工程修改冲突将拒绝。'; return }
    if (result.state === 'failed') { editorError.value = result.error; return }
  } catch (error) {
    editorError.value = error.message
    if (projectEditor.value?.session_id) { projectEditor.value.state = 'failed'; return }
  }
  if (!disposed) editorTimer = setTimeout(pollProjectEditor, 700)
}
async function openProjectEditor(index, live = false) {
  editorBusy.value = true; editorError.value = ''; editorMessage.value = ''
  try {
    const target = editorTarget(index)
    const snapshot = await api('POST', '/project/effect/read', target)
    const session_id = live ? store.transport.native_session_id : null
    if (live && (!store.transport.playing || !session_id)) throw new Error('请先开始含VST的持续试听，再打开实时实例编辑器。')
    const result = await editorRequest(live
      ? { action: 'open', session_id, ...target, path: snapshot.effect.path }
      : { action: 'open', path: snapshot.effect.path, parameters: snapshot.effect.parameters || {}, state_base64: snapshot.effect.state_base64 })
    projectEditor.value = { ...result, session_id, target, revision: snapshot.revision, label: `${ids.value.find(item => item.id === selectedId.value)?.name || selectedId.value} / 效果 ${index + 1}` }
    if (disposed) { await editorRequest({ action: 'close', id: result.id, session_id }); return }
    editorMessage.value = live ? '正在编辑实际试听实例；GUI修改立即作用于后续音频，尚未保存到工程。关闭后可确认保存。' : '独立编辑器已请求打开；关闭并回读后可显式应用，不会自动修改工程。'
    pollProjectEditor()
  } catch (error) { editorError.value = error.message }
  finally { editorBusy.value = false }
}
async function closeProjectEditor() {
  try { await editorRequest({ action: 'close', id: projectEditor.value.id, session_id: projectEditor.value.session_id }) }
  catch (error) { editorError.value = error.message }
}
async function applyProjectEditor() {
  const result = projectEditor.value
  if (!result || result.state !== 'completed') return
  editorBusy.value = true; editorError.value = ''
  try {
    if (result.session_id) actions.stop()
    await api('PATCH', '/project/effect', { ...result.target, expected_revision: result.revision,
      parameters: Object.fromEntries(result.parameters.map(parameter => [parameter.name, parameter.value])), state_base64: result.state_base64 })
    projectEditor.value = null
    await actions.refreshMixer()
    editorMessage.value = '参数与插件预设状态已保存到工程配置；请重新播放以载入状态。外部采样/文件不随预设携带。'
  } catch (error) { editorError.value = error.message === 'STALE_PROJECT' ? '工程在编辑器打开后已修改，未应用参数。请丢弃结果并重新打开编辑器。' : error.message }
  finally { editorBusy.value = false }
}
onUnmounted(() => {
  disposed = true
  clearTimeout(editorTimer)
  if (editorRunning.value) editorRequest({ action: 'close', id: projectEditor.value.id, session_id: projectEditor.value.session_id }).catch(() => {})
})
async function discoverVstPlugins() {
  try {
    const response = await fetch(apiUrl('/api/inference/vst/discover'))
    const result = await response.json()
    if (response.ok) availableVstPlugins.value = result.plugins || []
  } catch { /* Manual project-relative paths remain available. */ }
}
onMounted(discoverVstPlugins)
async function inspectVst(index) {
  const targetChannel=selectedId.value,effect=effects.value[index],path=effect.path
  try {
    const response=await fetch(apiUrl('/api/inference/vst/inspect'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
    const result=await response.json();if(!response.ok)throw new Error(result.detail||result.error||response.statusText)
    if(selectedId.value!==targetChannel||effects.value[index]?.path!==path)return
    vstMetadata.value={...vstMetadata.value,[`${targetChannel}:${index}:${path}`]:result.parameters||[]}
    vstErrors.value={...vstErrors.value,[`${targetChannel}:${index}`]:''}
  } catch(error) { vstErrors.value={...vstErrors.value,[`${targetChannel}:${index}`]:error.message} }
}
function addVst(){const path=draftVstPath.value.trim();if(!path)return;const key=selectedId.value==='master'||selectedId.value.startsWith('bus:')?'effects':'fx';patchChannel({[key]:[...effects.value,{type:'vst3',enabled:false,path,parameters:{},block_frames:4096,tail_seconds:0}]});draftVstPath.value='plugins/'}
function vstParameter(index,name,value){patchEffect(index,{parameters:{...(effects.value[index].parameters||{}),[name]:value}})}
function addBus(){const config=structuredClone(toRaw(props.modelValue||defaultMixerConfig()));config.buses ||= [];let id=`bus-${Date.now().toString(36)}`;while(config.buses.some(b=>b.id===id))id+='x';config.buses.push({id,name:`Bus ${config.buses.length+1}`,volume:1,pan:0,output:'master',sends:[],effects:[]});emit('update:modelValue',config);selectedId.value=`bus:${id}`}
function removeBus(){if(!selectedId.value.startsWith('bus:'))return;const id=selectedId.value.slice(4),config=structuredClone(toRaw(props.modelValue||defaultMixerConfig()));config.buses=(config.buses||[]).filter(b=>b.id!==id);emit('update:modelValue',config);selectedId.value='master'}
const outputChoices=computed(()=>[{id:'master',name:'Master'},...(props.modelValue?.buses||[]).filter(b=>`bus:${b.id}`!==selectedId.value).map(b=>({id:b.id,name:b.name||b.id}))])
function updateSend(index,patch){const sends=[...(channel.value?.sends||[])];sends[index]={...sends[index],...patch};patchChannel({sends})}
function addSend(){const current=selectedId.value.startsWith('bus:')?selectedId.value.slice(4):null;const dest=(props.modelValue?.buses||[]).find(b=>b.id!==current)?.id||'master';patchChannel({sends:[...(channel.value?.sends||[]),{to:dest,level:.25}]})}
function removeSend(index){patchChannel({sends:(channel.value?.sends||[]).filter((_,i)=>i!==index)})}
</script>
<template>
  <section class="mixer-panel" aria-label="混音器">
    <header><strong>混音器</strong><select v-model="selectedId" aria-label="混音通道"><option v-for="item in ids" :key="item.id" :value="item.id">{{ item.name }}</option></select></header>
    <div v-if="channel" class="channel">
      <div class="channel-controls">
        <label v-if="selectedId !== 'master'">音量 <input type="range" min="0" max="2" step="0.01" :value="channel.volume ?? 1" @input="patchChannel({ volume: Number($event.target.value) })"> {{ Number(channel.volume ?? 1).toFixed(2) }}</label>
        <label v-if="selectedId !== 'master' && !selectedId.startsWith('bus:')">声像 <input type="range" min="-1" max="1" step="0.01" :value="channel.pan ?? 0" @input="patchChannel({ pan: Number($event.target.value) })"></label>
        <div v-if="selectedId.startsWith('bus:')" class="routing"><label>输出 <select :value="channel.output || 'master'" @change="patchChannel({ output: $event.target.value })"><option v-for="c in outputChoices" :key="c.id" :value="c.id">{{ c.name }}</option></select></label><button @click="removeBus">删除总线</button></div>
        <div v-if="selectedId !== 'master' && !selectedId.startsWith('bus:')" class="routing"><label>总线 <select :value="channel.bus || 'master'" @change="patchChannel({ bus: $event.target.value })"><option value="master">Master</option><option v-for="b in modelValue.buses || []" :key="b.id" :value="b.id">{{ b.name || b.id }}</option></select></label></div>
        <div v-if="selectedId !== 'master'" class="sends"><strong>Sends</strong><div v-for="(send,i) in channel.sends||[]" :key="i"><select :value="send.to" @change="updateSend(i,{to:$event.target.value})"><option value="master">Master</option><option v-for="b in modelValue.buses||[]" :key="b.id" :value="b.id">{{ b.name||b.id }}</option></select><input type="range" min="0" max="1" step="0.01" :value="send.level??1" @input="updateSend(i,{level:Number($event.target.value)})"><button @click="removeSend(i)">×</button></div><button @click="addSend">+ Send</button></div>
      </div>
      <div class="effects" aria-label="按处理顺序排列的效果链"><article v-for="(effect, i) in effects" :key="i" class="effect" :class="{ 'vst-effect': effect.type === 'vst3' }">
        <div class="effect-title"><label><input type="checkbox" :checked="effect.enabled" @change="patchEffect(i,{enabled:$event.target.checked})"> {{ effectLabel(effect.type) }}</label><button @click="removeEffect(i)" aria-label="移除效果">×</button></div>
        <template v-if="effect.type==='eq3'"><label v-for="[key,title,min,max] in [['low_db','低频 dB',-24,24],['mid_db','中频 dB',-24,24],['high_db','高频 dB',-24,24]]" :key="key">{{ title }} <input type="range" :min="min" :max="max" step="0.5" :value="effect[key]" @input="numeric(i,key,$event)"></label></template>
        <template v-else-if="effect.type==='reverb'"><label>湿度 <input type="range" min="0" max="1" step="0.01" :value="effect.wet" @input="numeric(i,'wet',$event)"></label><label>房间 <input type="range" min="0" max="1" step="0.01" :value="effect.room" @input="numeric(i,'room',$event)"></label></template>
        <template v-else-if="effect.type==='compressor'"><label>阈值（dB） <input type="range" min="-60" max="0" step="1" :value="effect.threshold_db" @input="numeric(i,'threshold_db',$event)"></label><label>压缩比 <input type="range" min="1" max="20" step="0.5" :value="effect.ratio" @input="numeric(i,'ratio',$event)"></label><label>启动时间（毫秒） <input type="range" min="0.1" max="200" step="0.1" :value="effect.attack_ms" @input="numeric(i,'attack_ms',$event)"></label><label>释放时间（毫秒） <input type="range" min="1" max="2000" step="1" :value="effect.release_ms" @input="numeric(i,'release_ms',$event)"></label></template>
        <template v-if="effect.type==='vst3'">
          <label class="vst-path">插件路径（相对 plugins/） <input type="text" :value="effect.path" @change="patchEffect(i,{path:$event.target.value,parameters:{},state_base64:null})" placeholder="plugins/Effect.vst3"></label>
          <select aria-label="项目内插件" @change="patchEffect(i,{path:$event.target.value,parameters:{},state_base64:null})" :value="effect.path"><option value="">选择项目内插件</option><option v-for="plugin in availableVstPlugins" :key="plugin.path" :value="plugin.path">{{ plugin.name }} — {{ plugin.path }}</option></select>
          <button @click="inspectVst(i)" :disabled="!effect.path">读取参数</button>
          <button @click="openProjectEditor(i)" :disabled="!effect.path || editorBusy || !!projectEditor">原生编辑器（工程配置）</button>
          <button @click="openProjectEditor(i, true)" :disabled="!effect.path || editorBusy || !!projectEditor || !store.transport.playing || !store.transport.native_session_id">实时实例编辑器（试听中）</button>
          <small v-if="effect.state_base64">已保存插件原生预设状态；公开参数调整覆盖预设值，更新预设须重新播放。</small>
          <div class="vst-settings"><label>分块帧数 <input type="number" min="1" max="65536" :value="effect.block_frames??4096" @change="patchEffect(i,{block_frames:Number($event.target.value)})"></label><label>尾音秒数 <input type="number" min="0" max="10" step="0.1" :value="effect.tail_seconds??0" @change="patchEffect(i,{tail_seconds:Number($event.target.value)})"></label></div>
          <EffectParameterGrid v-if="vstMetadata[`${selectedId}:${i}:${effect.path}`]?.length" :parameters="vstMetadata[`${selectedId}:${i}:${effect.path}`]" :values="effect.parameters || {}" @change="(name, value) => vstParameter(i,name,value)" />
          <small class="error">{{ vstErrors[`${selectedId}:${i}`] }}</small>
        </template>
      </article></div>
      <div class="add"><button v-for="kind in ['eq3','reverb','compressor']" :key="kind" @click="addEffect(kind)">+ {{ effectLabel(kind) }}</button></div>
      <p class="offline-note">独立编辑器不影响试听；实时实例编辑器直接修改当前持续会话的插件，缓冲后可听，但不自动保存工程。关闭后确认保存会停止试听；停止/跳转会释放实时窗口，未回读结果不保留。两种编辑器均不内嵌，工程修改冲突会拒绝保存。</p>
      <div class="add vst-add"><input v-model="draftVstPath" placeholder="插件路径（相对 plugins/），如 plugins/Effect.vst3"><button :disabled="!draftVstPath.trim()" @click="addVst">+ VST3</button></div>
    </div>
    <p v-else>选择一个轨道或 Master 编辑效果链。</p>
    <section v-if="projectEditor" class="editor-result" aria-live="polite">
      <strong>{{ projectEditor.session_id ? '实时实例编辑目标' : '原生编辑目标' }}：{{ projectEditor.label }}</strong>
      <button v-if="editorRunning" @click="closeProjectEditor">关闭编辑器并读回</button>
      <template v-if="projectEditor.state === 'completed'">
        <p>已读回 {{ projectEditor.parameters.length }} 个参数与插件原生预设状态，尚未应用。</p>
        <button :disabled="editorBusy" @click="applyProjectEditor">{{ projectEditor.session_id ? '停止试听并保存到工程' : '确认应用到原目标' }}</button>
      </template>
      <button v-if="!editorRunning" :disabled="editorBusy" @click="projectEditor = null; editorError = ''; editorMessage = ''">{{ projectEditor.session_id ? '不保存到工程（试听实例已修改）' : '丢弃编辑结果' }}</button>
    </section>
    <p v-if="editorMessage" class="offline-note" role="status">{{ editorMessage }}</p>
    <p v-if="editorError" class="error" role="alert">{{ editorError }}</p>
  </section>
</template>
<style scoped>
.mixer-panel{box-sizing:border-box;min-width:0;background:#181b20;color:#c8cdd5;padding:8px;font-size:12px}header,.effect-title{display:flex;align-items:center;justify-content:space-between;gap:8px}select,.effect button,.add button,.effect input[type=number],.effect input[type=text],.add input{box-sizing:border-box;max-width:100%;background:#171c22;border:1px solid #394351;color:#dce2eb;border-radius:4px;padding:6px}.mixer-panel>header select{height:32px;width:min(240px,65%)}button{cursor:pointer}.channel{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;min-width:0;margin-top:8px}.channel>label,.routing,.sends{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.channel input[type=range],.sends input[type=range]{width:min(240px,60vw);accent-color:#8b70e8}.effects{display:grid;grid-template-columns:minmax(0,1fr);gap:10px;min-width:0}.effect{box-sizing:border-box;min-width:0;width:100%;background:#20242b;border:1px solid #343a44;border-radius:5px;padding:10px;display:grid;align-content:start;gap:8px}.effect-title label{font-weight:600}.effect-title button{cursor:pointer}.effect input[type=range]{accent-color:#8b70e8}.effect>label:not(.vst-path){display:flex;gap:5px;align-items:center;flex-wrap:wrap}.effect>label:not(.vst-path) input[type=range]{flex:1;min-width:100px}.vst-effect{grid-column:1/-1}.vst-path{display:grid;gap:4px;min-width:0}.vst-path input{width:100%;height:32px}.vst-effect>select{width:min(360px,100%);height:32px}.vst-settings{display:flex;gap:12px;flex-wrap:wrap}.vst-settings label{display:grid;gap:4px}.sends>div{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.add{display:flex;gap:5px;flex-wrap:wrap}.vst-add input{flex:1;min-width:min(260px,100%);height:32px}.add button{cursor:pointer}.offline-note{margin:0;color:#9aa3af}.error{color:#ff9388}@media(min-width:900px){.effects{grid-template-columns:repeat(2,minmax(0,1fr))}.vst-effect{grid-column:1/-1}}
.channel-controls{display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:12px;background:#1c2027;border:1px solid #303742;border-radius:6px}.channel-controls>label,.routing label{display:flex;align-items:center;gap:8px;white-space:nowrap}.channel-controls button,.routing button,.sends button{min-height:32px;border:1px solid #394351;border-radius:4px;background:#252d39;color:#dce2eb;padding:5px 9px;font:inherit}.channel-controls select{height:32px}.channel-controls input[type=range]{width:160px}.effect-title input[type=checkbox]{accent-color:#8b70e8}.effect>button{justify-self:start;min-height:32px;padding:5px 14px}.sends strong{color:#aab4c2;font-weight:500}
.editor-result{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px;border:1px solid #50466d;border-radius:6px;background:#242130}.editor-result button{min-height:32px;padding:5px 10px;background:#353044;color:#eee7ff;border:1px solid #66547f;border-radius:4px;cursor:pointer}.editor-result button:disabled{opacity:.45;cursor:not-allowed}
</style>
