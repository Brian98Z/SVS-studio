<template>
  <section class="tempo-editor" aria-label="Tempo map">
    <header>
      <strong>Tempo map</strong>
      <button type="button" @click="addPoint">Add point</button>
    </header>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-for="(point, index) in points" :key="point.id" class="tempo-row">
      <label>Tick <input v-model.number="point.tick" type="number" min="0" step="1" :disabled="index === 0"></label>
      <label>BPM <input v-model.number="point.bpm" type="number" min="1" max="999" step="0.1"></label>
      <label>To next
        <select v-model="point.curve" :disabled="index === points.length - 1">
          <option value="step">Step</option>
          <option value="linear">Linear</option>
        </select>
      </label>
      <button v-if="index" type="button" class="remove" aria-label="Remove tempo point" @click="removePoint(index)">Remove</button>
    </div>
    <footer><button type="button" :disabled="saving" @click="save">{{ saving ? 'Saving…' : 'Apply tempo map' }}</button></footer>
  </section>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({ tempos: { type: Array, required: true } })
const emit = defineEmits(['saved'])
const points = ref([])
const saving = ref(false)
const error = ref('')
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
  error.value = ''
  const tempos = points.value.map(({ tick, bpm, curve }) => ({ tick, bpm, curve }))
  if (!tempos.length || tempos[0].tick !== 0 || tempos.some((p, i) => !Number.isInteger(p.tick) || p.tick < 0 || !Number.isFinite(p.bpm) || p.bpm <= 0 || (i > 0 && p.tick <= tempos[i - 1].tick))) {
    error.value = 'Tempo points must start at tick 0 and have increasing ticks and positive BPM.'
    return
  }
  saving.value = true
  try {
    const response = await fetch('/api/project/tempos', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tempos }),
    })
    const result = await response.json()
    if (!response.ok) throw new Error(result.detail || result.error || 'Could not update tempo map.')
    emit('saved', result.tempos || tempos)
  } catch (e) {
    error.value = e.message || 'Could not update tempo map.'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.tempo-editor { display: grid; gap: 10px; padding: 12px; color: #e5e7eb; background: #171a20; border: 1px solid #303641; border-radius: 8px; }
header, footer, .tempo-row { display: flex; align-items: center; gap: 10px; }
header, footer { justify-content: space-between; }
.tempo-row { flex-wrap: wrap; padding: 8px 0; border-top: 1px solid #292e37; }
label { display: grid; gap: 4px; color: #aab2bf; font-size: 12px; }
input, select, button { min-height: 30px; color: inherit; background: #222731; border: 1px solid #3b4350; border-radius: 4px; padding: 4px 8px; }
input { width: 92px; }
button { cursor: pointer; }
button:disabled { opacity: .55; cursor: default; }
.remove, .error { color: #fca5a5; }
.error { margin: 0; font-size: 13px; }
</style>
