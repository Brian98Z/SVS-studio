<template>
  <section class="tempo-editor" aria-label="速度图">
    <header>
      <div><h2>速度图</h2><p>以时间点控制演奏速度；480 刻度为一拍。</p></div>
      <button type="button" @click="addPoint">＋ 添加速度点</button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p class="hint">从第 0 刻度开始。阶跃保持当前速度，线性则逐渐变化到下一个速度点。</p>
    <div v-for="(point, index) in points" :key="point.id" class="tempo-row">
      <span class="point-index">{{ String(index + 1).padStart(2, '0') }}</span>
      <label>位置（刻度） <input v-model.number="point.tick" type="number" min="0" step="1" :disabled="index === 0"></label>
      <label>速度（BPM） <input v-model.number="point.bpm" type="number" min="1" max="999" step="0.1"></label>
      <label>到下一点的过渡
        <select v-model="point.curve" :disabled="index === points.length - 1">
          <option value="step">阶跃 · 保持当前速度</option>
          <option value="linear">线性 · 平滑变速</option>
        </select>
      </label>
      <button v-if="index" type="button" class="remove" :aria-label="`删除速度点 ${index + 1}`" @click="removePoint(index)">删除</button>
      <span v-else class="origin-label">起点</span>
    </div>
    <footer><span class="hint">共 {{ points.length }} 个速度点</span><button type="button" class="primary" :disabled="saving" @click="save">{{ saving ? '正在保存…' : '应用速度图' }}</button></footer>
    <p v-if="message" class="success" role="status">{{ message }}</p>
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'
import { apiUrl } from '../audio/origin.js'

const props = defineProps({ tempos: { type: Array, required: true } })
const emit = defineEmits(['saved'])
const points = ref([])
const saving = ref(false)
const error = ref('')
const message = ref('')
let nextId = 0

function loadPoints(tempos) {
  points.value = (tempos || []).map(p => ({ ...p, curve: p.curve || 'step', id: nextId++ }))
}
watch(() => props.tempos, loadPoints, { immediate: true, deep: true })

function addPoint() {
  const last = points.value.at(-1)
  const tick = last ? last.tick + 480 : 0
  points.value.push({ id: nextId++, tick, bpm: last?.bpm || 120, curve: 'step' })
}
function removePoint(index) { points.value.splice(index, 1) }

async function save() {
  error.value = ''; message.value = ''
  const tempos = points.value.map(({ tick, bpm, curve }) => ({ tick, bpm, curve }))
  if (!tempos.length || tempos[0].tick !== 0 || tempos.some((p, i) => !Number.isInteger(p.tick) || p.tick < 0 || !Number.isFinite(p.bpm) || p.bpm <= 0 || (i > 0 && p.tick <= tempos[i - 1].tick))) {
    error.value = '速度图必须从第 0 刻度开始，位置严格递增，BPM 必须为正数。'
    return
  }
  saving.value = true
  try {
    const response = await fetch(apiUrl('/api/project/tempos'), {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tempos }),
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || result.error || '无法更新速度图。')
    emit('saved', result.tempos || tempos)
    message.value = '速度图已保存并应用。'
  } catch (e) {
    error.value = e.message || '无法更新速度图。'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.tempo-editor{display:grid;gap:16px;max-width:960px;margin:0 auto;padding:20px;color:#e5e7eb;background:#1c2027;border:1px solid #303742;border-radius:8px;min-width:0;font:13px/1.5 system-ui,sans-serif}
header,footer{display:flex;align-items:center;justify-content:space-between;gap:16px}header h2{margin:0;font-size:17px}header p{margin:5px 0 0;color:#929baa}.hint{margin:0;color:#929baa;font-size:12px}
.tempo-row{display:grid;grid-template-columns:32px minmax(90px,1fr) minmax(100px,1fr) minmax(180px,1.5fr) 56px;align-items:end;gap:12px;padding:14px 0;border-top:1px solid #303742}.point-index{align-self:center;color:#738094;font-variant-numeric:tabular-nums}.origin-label{align-self:center;color:#929baa;text-align:center}
label{display:grid;gap:6px;color:#aeb7c5;font-size:12px;min-width:0}input,select,button{box-sizing:border-box;min-width:0;height:36px;color:inherit;background:#12161d;border:1px solid #3a4351;border-radius:5px;padding:7px 10px;font:inherit}input,select{width:100%}input:focus,select:focus{outline:2px solid #8b70e8;outline-offset:1px}button{cursor:pointer;white-space:nowrap;background:#2a3240}button:hover:not(:disabled){background:#354157}button:disabled,input:disabled,select:disabled{opacity:.5;cursor:default}.primary{background:#39445c;border-color:#505e7b}.remove,.error{color:#ff9388}.error,.success{margin:0}.success{color:#86d6a2}footer{border-top:1px solid #303742;padding-top:16px}
@media(max-width:640px){.tempo-editor{padding:14px}.tempo-row{grid-template-columns:24px minmax(0,1fr) minmax(0,1fr)}.tempo-row label:nth-of-type(3){grid-column:2 / -1}.tempo-row button,.origin-label{grid-column:3;justify-self:end}header{align-items:start;flex-wrap:wrap}header button{margin-left:auto}}
</style>
