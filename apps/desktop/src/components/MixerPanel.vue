<script setup>
import { computed, toRaw } from 'vue'
import { defaultMixerConfig } from '../audio/fx.js'

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
      <label v-if="selectedId !== 'master'">音量 <input type="range" min="0" max="2" step="0.01" :value="channel.volume ?? 1" @input="patchChannel({ volume: Number($event.target.value) })"> {{ Number(channel.volume ?? 1).toFixed(2) }}</label>
      <label v-if="selectedId !== 'master' && !selectedId.startsWith('bus:')">声像 <input type="range" min="-1" max="1" step="0.01" :value="channel.pan ?? 0" @input="patchChannel({ pan: Number($event.target.value) })"></label>
      <div v-if="selectedId.startsWith('bus:')" class="routing"><label>输出 <select :value="channel.output || 'master'" @change="patchChannel({ output: $event.target.value })"><option v-for="c in outputChoices" :key="c.id" :value="c.id">{{ c.name }}</option></select></label><button @click="removeBus">删除总线</button></div>
      <div v-if="selectedId !== 'master' && !selectedId.startsWith('bus:')" class="routing"><label>总线 <select :value="channel.bus || 'master'" @change="patchChannel({ bus: $event.target.value })"><option value="master">Master</option><option v-for="b in modelValue.buses || []" :key="b.id" :value="b.id">{{ b.name || b.id }}</option></select></label></div>
      <div v-if="selectedId !== 'master'" class="sends"><strong>Sends</strong><div v-for="(send,i) in channel.sends||[]" :key="i"><select :value="send.to" @change="updateSend(i,{to:$event.target.value})"><option value="master">Master</option><option v-for="b in modelValue.buses||[]" :key="b.id" :value="b.id">{{ b.name||b.id }}</option></select><input type="range" min="0" max="1" step="0.01" :value="send.level??1" @input="updateSend(i,{level:Number($event.target.value)})"><button @click="removeSend(i)">×</button></div><button @click="addSend">+ Send</button></div>
      <div class="routing"><button @click="addBus">+ 总线</button></div>
      <div class="effects"><article v-for="(effect, i) in effects" :key="i" class="effect">
        <div class="effect-title"><label><input type="checkbox" :checked="effect.enabled" @change="patchEffect(i, { enabled: $event.target.checked })"> {{ effect.type.toUpperCase() }}</label><button @click="removeEffect(i)" aria-label="移除效果">×</button></div>
        <template v-if="effect.type === 'eq3'"><label v-for="[key, title, min, max] in [['low_db','低频 dB',-24,24],['mid_db','中频 dB',-24,24],['high_db','高频 dB',-24,24]]" :key="key">{{ title }} <input type="range" :min="min" :max="max" step="0.5" :value="effect[key]" @input="numeric(i,key,$event)"></label></template>
        <template v-else-if="effect.type === 'reverb'"><label>湿度 <input type="range" min="0" max="1" step="0.01" :value="effect.wet" @input="numeric(i,'wet',$event)"></label><label>房间 <input type="range" min="0" max="1" step="0.01" :value="effect.room" @input="numeric(i,'room',$event)"></label></template>
        <template v-else><label>阈值 dB <input type="range" min="-60" max="0" step="1" :value="effect.threshold_db" @input="numeric(i,'threshold_db',$event)"></label><label>压缩比 <input type="range" min="1" max="20" step="0.5" :value="effect.ratio" @input="numeric(i,'ratio',$event)"></label><label>Attack ms <input type="range" min="0.1" max="200" step="0.1" :value="effect.attack_ms" @input="numeric(i,'attack_ms',$event)"></label><label>Release ms <input type="range" min="1" max="2000" step="1" :value="effect.release_ms" @input="numeric(i,'release_ms',$event)"></label></template>
      </article></div>
      <div class="add"><button v-for="kind in ['eq3','reverb','compressor']" :key="kind" @click="addEffect(kind)">+ {{ kind }}</button></div>
    </div>
    <p v-else>选择一个轨道或 Master 编辑效果链。</p>
  </section>
</template>

<style scoped>
.mixer-panel{background:#181b20;color:#c8cdd5;padding:8px;overflow:auto;font-size:12px}header,.effect-title{display:flex;align-items:center;justify-content:space-between;gap:8px}select,.effect button,.add button{background:#262b33;border:1px solid #343a44;color:#c8cdd5;border-radius:3px;padding:4px}.channel{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;margin-top:8px}.channel>label,.effect label{display:flex;gap:5px;align-items:center}.channel input[type=range]{width:90px;accent-color:#8b70e8}.effects{display:flex;gap:8px;flex-wrap:wrap}.effect{min-width:175px;background:#20242b;border:1px solid #343a44;border-radius:4px;padding:6px;display:grid;gap:5px}.effect-title label{font-weight:600}.effect-title button{cursor:pointer}.add{display:flex;gap:5px}.add button{cursor:pointer}
</style>
