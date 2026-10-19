<script setup>
import { computed, ref } from 'vue'
import { store, api } from '../stores/project.js'

const track = computed(() => store.project.tracks.find(item => item.id === store.selection.trackId))
const noteIds = computed(() => [...store.selection.noteIds])
const vibratoEnabled = ref(true)
const depthCents = ref(35)
const frequencyHz = ref(5.5)
const delayMs = ref(120)
const onsetMs = ref(80)
const releaseMs = ref(100)
const attackMs = ref(0)
const decayMs = ref(0)
const kind = ref('sung')
const busy = ref(false)
const error = ref('')
const message = ref('')

async function apply() {
  if (!track.value || !noteIds.value.length || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    await api('POST', `/track/${encodeURIComponent(track.value.id)}/expression`, {
      note_ids: noteIds.value,
      vibrato: vibratoEnabled.value ? { depth_cents: Number(depthCents.value), frequency_hz: Number(frequencyHz.value), delay_ms: Number(delayMs.value), onset_ms: Number(onsetMs.value), release_ms: Number(releaseMs.value) } : null,
      attack_ms: Number(attackMs.value), decay_ms: Number(decayMs.value), kind: kind.value,
    })
    message.value = '已提交所选音符的表达设置。'
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <section class="expression-panel" aria-labelledby="expression-title">
    <header><div><h2 id="expression-title">演唱表达</h2><p>设置所选音符的颤音、辅音时值与事件类型</p></div></header>
    <p v-if="!track || !noteIds.length" class="hint">请先选择轨道和一个或多个音符。</p>
    <template v-else>
      <div class="grid">
        <label>发声类型<select v-model="kind"><option value="sung">歌声</option><option value="breath">真实呼吸 AP</option></select></label>
        <label class="check"><input v-model="vibratoEnabled" type="checkbox">启用颤音</label>
        <label>深度（cent）<input v-model.number="depthCents" type="number" min="0" max="300" step="1" :disabled="!vibratoEnabled"></label>
        <label>频率（Hz）<input v-model.number="frequencyHz" type="number" min="0.1" max="15" step="0.1" :disabled="!vibratoEnabled"></label>
        <label>延迟（ms）<input v-model.number="delayMs" type="number" min="0" step="1" :disabled="!vibratoEnabled"></label>
        <label>起音渐入（ms）<input v-model.number="onsetMs" type="number" min="0" step="1" :disabled="!vibratoEnabled"></label>
        <label>收尾渐出（ms）<input v-model.number="releaseMs" type="number" min="0" step="1" :disabled="!vibratoEnabled"></label>
        <label>辅音起始调整（ms）<input v-model.number="attackMs" type="number" step="1"></label>
        <label>辅音尾部调整（ms）<input v-model.number="decayMs" type="number" step="1"></label>
      </div>
      <button class="apply" :disabled="busy" @click="apply">{{ busy ? '提交中…' : `应用到 ${noteIds.length} 个音符` }}</button>
    </template>
    <p v-if="message" class="success" role="status">{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.expression-panel { padding: 16px; color: #e3e7ed; background: #1b1f25; }
header h2 { margin: 0; font-size: 15px; } header p,.hint { margin: 5px 0 14px; color: #8993a0; font-size: 12px; }
.grid { display: grid; grid-template-columns: repeat(2,minmax(120px,1fr)); gap: 10px 12px; }
label { display: grid; gap: 4px; color: #aab3bf; font-size: 11px; }
input:not([type=checkbox]),select { min-width: 0; padding: 6px 7px; color: #e3e7ed; background: #252b34; border: 1px solid #39414d; border-radius: 4px; }
.check { display: flex; align-items: center; gap: 7px; } .check input { accent-color: #9477e8; }
.apply { margin-top: 14px; padding: 7px 12px; color: white; background: #7256c7; border: 0; border-radius: 4px; cursor: pointer; }
.apply:disabled { opacity: .55; cursor: wait; } .success { color: #8bd4a4; } .error { color: #ff9292; }
@media(max-width:420px) { .grid { grid-template-columns: 1fr; } }
</style>
