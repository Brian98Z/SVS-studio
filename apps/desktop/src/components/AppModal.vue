<script setup>
// 应用内模态对话框（替代浏览器 prompt/confirm）。
// 用法见 stores/ui.js 的 uiPrompt/uiConfirm/uiSelect。
import { ref, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  title: String,
  label: String,
  placeholder: String,
  initial: String,
  mode: { type: String, default: 'prompt' }, // prompt | confirm | select
  options: { type: Array, default: () => [] }, // select 模式: [{value,label}]
  message: String,
})
const emit = defineEmits(['done'])
const value = ref('')
const inputRef = ref(null)

function finish(v) { emit('done', v) }
function onKey(e) {
  if (e.key === 'Enter' && props.mode === 'prompt') finish(value.value)
  if (e.key === 'Escape') finish(null)
}
onMounted(() => {
  value.value = props.initial || ''
  setTimeout(() => inputRef.value?.focus(), 30)
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div class="overlay" @mousedown.self="finish(null)">
    <div class="dialog">
      <div class="dlg-title">{{ title }}</div>
      <div v-if="label" class="dlg-label">{{ label }}</div>
      <div v-if="message" class="dlg-msg">{{ message }}</div>
      <input v-if="mode === 'prompt'" ref="inputRef" v-model="value" :placeholder="placeholder"
        class="dlg-input" spellcheck="false" />
      <div v-if="mode === 'select'" class="dlg-options">
        <button v-for="o in options" :key="o.value" class="dlg-opt" @click="finish(o.value)">{{ o.label }}</button>
      </div>
      <div class="dlg-btns">
        <button v-if="mode !== 'select'" class="btn primary" @click="finish(value)">确定</button>
        <button class="btn" @click="finish(null)">取消</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay { position: fixed; inset: 0; background: rgba(8,10,14,.62); backdrop-filter: blur(2px);
  display: flex; align-items: flex-start; justify-content: center; padding-top: 18vh; z-index: 1000; }
.dialog { width: 460px; max-width: 92vw; background: #1e222a; border: 1px solid #3a4150;
  border-radius: 10px; box-shadow: 0 18px 60px rgba(0,0,0,.55); padding: 18px 20px 16px;
  color: #d8dce2; }
.dlg-title { font-size: 15px; font-weight: 600; color: #e8ecf2; margin-bottom: 10px; }
.dlg-label { font-size: 13px; color: #9aa3ad; margin-bottom: 8px; }
.dlg-msg { font-size: 13px; color: #b8bec8; margin-bottom: 8px; white-space: pre-wrap; }
.dlg-input { width: 100%; box-sizing: border-box; background: #14171c; color: #e8ecf2;
  border: 1px solid #3a4150; border-radius: 6px; padding: 9px 12px; font-size: 14px; outline: none; }
.dlg-input:focus { border-color: #5b8def; }
.dlg-options { display: flex; flex-direction: column; gap: 6px; margin: 6px 0 2px; }
.dlg-opt { text-align: left; background: #262b33; color: #d8dce2; border: 1px solid #343a44;
  border-radius: 6px; padding: 10px 12px; font-size: 13px; cursor: pointer; }
.dlg-opt:hover { background: #313742; border-color: #5b8def; }
.dlg-btns { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.btn { background: #262b33; color: #d8dce2; border: 1px solid #343a44; border-radius: 6px;
  padding: 7px 16px; font-size: 13px; cursor: pointer; }
.btn:hover { background: #313742; }
.btn.primary { background: #3b6fd4; border-color: #3b6fd4; color: #fff; }
.btn.primary:hover { background: #4a7de0; }
</style>
