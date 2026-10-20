<script setup>
import { computed } from 'vue'
import { parameterLabel, parameterValueLabel } from '../audio/effect-labels.js'

const props = defineProps({ parameters: { type: Array, default: () => [] }, values: { type: Object, default: () => ({}) } })
const emit = defineEmits(['change'])
const groups = computed(() => {
  const order = ['band_1', 'band_2', 'band_3', 'band_4', 'hp', 'lp', 'wide_band', 'general']
  const grouped = new Map()
  for (const parameter of props.parameters) {
    const name = String(parameter.name || '')
    const group = order.find(key => new RegExp(`(^|[^a-z0-9])${key}([^a-z0-9]|$)`, 'i').test(name)) || 'general'
    if (!grouped.has(group)) grouped.set(group, [])
    grouped.get(group).push(parameter)
  }
  return order.filter(group => grouped.has(group)).map(group => ({ id: group, title: group === 'general' ? '通用参数' : ({ hp: '高通', lp: '低通', wide_band: '宽频段', band_1: '频段 1', band_2: '频段 2', band_3: '频段 3', band_4: '频段 4' })[group], parameters: grouped.get(group) }))
})
function value(parameter) { return props.values[parameter.name] ?? parameter.next ?? parameter.value ?? (parameter.type === 'boolean' ? false : parameter.type === 'string' ? '' : 0) }
function change(parameter, event) {
  const raw = event.target.type === 'checkbox' ? event.target.checked : event.target.value
  const selected = parameter.valid_values?.find(option => String(option) === String(raw))
  emit('change', parameter.name, parameter.type === 'boolean' ? Boolean(raw) : parameter.type === 'string' ? raw : parameter.valid_values?.length ? selected ?? raw : Number(raw))
}
function isRange(parameter) { return !parameter.valid_values?.length && !['boolean', 'string'].includes(parameter.type) && parameter.min_value !== undefined && parameter.max_value !== undefined && parameter.max_value > parameter.min_value }
</script>

<template>
  <div class="parameter-groups">
    <section v-for="group in groups" :key="group.id" class="parameter-group">
      <h3>{{ group.title }}</h3>
      <div class="parameter-grid">
        <label v-for="parameter in group.parameters" :key="parameter.name" class="parameter" :title="parameter.name" :class="{ compact: ['boolean','bool'].includes(parameter.type) }">
          <span class="parameter-label">{{ parameterLabel(parameter.name) }}<small v-if="parameter.min_value !== undefined && !isRange(parameter)">{{ parameter.min_value }}–{{ parameter.max_value }}</small></span>
          <input v-if="['boolean','bool'].includes(parameter.type)" class="boolean-input" type="checkbox" :checked="value(parameter)" @change="change(parameter, $event)">
          <select v-else-if="parameter.valid_values?.length" :value="value(parameter)" @change="change(parameter, $event)"><option v-for="option in parameter.valid_values" :key="String(option)" :value="option">{{ parameterValueLabel(option) }}</option></select>
          <input v-else-if="['string','str'].includes(parameter.type)" type="text" :value="value(parameter)" @change="change(parameter, $event)">
          <template v-else-if="isRange(parameter)"><input class="parameter-range" type="range" :min="parameter.min_value" :max="parameter.max_value" :step="parameter.step || 'any'" :value="value(parameter)" @input="change(parameter, $event)"><output>{{ parameterValueLabel(value(parameter)) }}</output></template>
          <input v-else type="number" :min="parameter.min_value" :max="parameter.max_value" :step="parameter.step || 'any'" :value="value(parameter)" @change="change(parameter, $event)">
        </label>
      </div>
    </section>
  </div>
</template>

<style scoped>
.parameter-groups{display:grid;gap:12px;min-width:0}.parameter-group{min-width:0}.parameter-group h3{margin:0 0 6px;color:#aab4c2;font-size:11px;font-weight:600}.parameter-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.parameter{display:grid;grid-template-columns:minmax(0,1fr);align-content:start;gap:5px;min-width:0;padding:8px;background:#20252c;border:1px solid #303741;border-radius:5px}.parameter-label{min-width:0;overflow-wrap:anywhere;color:#cbd2dc}.parameter-label small{display:block;color:#87909c;font-size:10px}.parameter input:not([type=checkbox]),.parameter select{box-sizing:border-box;width:100%;min-width:0;height:32px;padding:5px 8px;border:1px solid #394351;border-radius:4px;background:#171c22;color:#e4e9f0;font:inherit}.parameter select{height:32px;max-width:100%;padding:4px 7px}.parameter .parameter-range{height:18px!important;padding:0!important;border:0!important;background:transparent!important;accent-color:#8b70e8}.parameter output{justify-self:end;color:#abb7c8;font-variant-numeric:tabular-nums}.parameter.compact{grid-template-columns:minmax(0,1fr) auto;align-items:center}.boolean-input{width:16px;height:16px;margin:0;accent-color:#8b70e8}@media(max-width:900px){.parameter-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:520px){.parameter-grid{grid-template-columns:minmax(0,1fr)}}
</style>
