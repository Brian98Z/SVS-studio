<script setup>
import { computed, ref, watch } from 'vue'
import { store, actions } from '../stores/project.js'

const language = ref('zh')
const preview = ref([])
const override = ref('')
const busy = ref(false)
const error = ref('')
const message = ref('')
const selected = computed(() => {
  const track = store.project.tracks.find(item => item.id === store.selection.trackId)
  if (!track) return { track: null, notes: [] }
  const ids = new Set(store.selection.noteIds || [])
  return { track, notes: track.notes.filter(note => ids.has(note.id)) }
})
const lyric = computed(() => selected.value.notes.map(note => note.lyric).join(''))
watch([lyric, language], () => { preview.value = []; message.value = ''; error.value = '' })

async function previewPronunciation() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    const notes = selected.value.notes
    const result = await actions.api('POST', '/inference/g2p', {
      text: lyric.value, language: language.value, syllables: notes.map(note => note.lyric),
    })
    preview.value = result.per_note
  } catch (cause) { error.value = `发音预览失败：${cause.message}` }
  finally { busy.value = false }
}

async function applyPronunciation() {
  const notes = selected.value.notes
  if (!notes.length) return
  const groups = override.value.trim()
    ? override.value.split(/\s*\|\s*/).map(group => group.trim().split(/\s+/).filter(Boolean))
    : preview.value
  if (groups.length !== notes.length) {
    error.value = `需要为每个音符提供一组音素；当前选中 ${notes.length} 个音符，请用 | 分隔音符组。`
    return
  }
  busy.value = true; error.value = ''; message.value = ''
  try {
    for (const [index, note] of notes.entries()) {
      await actions.api('PATCH', `/track/${encodeURIComponent(selected.value.track.id)}/note/${encodeURIComponent(note.id)}`, { phonemes: groups[index] })
      note.phonemes = groups[index]
    }
    message.value = '发音已保存。'
  } catch (cause) { error.value = `保存发音失败：${cause.message}` }
  finally { busy.value = false }
}
</script>

<template>
  <section class="pronunciation" aria-labelledby="pronunciation-title">
    <header>
      <div><h2 id="pronunciation-title">发音与音素</h2><p>按语言预览音素，并为选中的音符保存发音设置。</p></div>
    </header>
    <label class="language">语言
      <select v-model="language">
        <option value="zh">普通话</option><option value="yue">粤语</option>
        <option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option>
      </select>
    </label>

    <section class="panel-section" aria-labelledby="selection-title">
      <div class="section-heading"><h3 id="selection-title">选中歌词与预览</h3><span>{{ selected.notes.length }} 个音符</span></div>
      <label>选中的歌词
        <textarea :value="lyric" readonly rows="2" placeholder="请先在时间线上选择一个或多个音符。" />
      </label>
      <p v-if="!selected.notes.length" class="hint">选择音符后即可预览或设置发音。</p>
      <div class="actions"><button :disabled="busy || !lyric" @click="previewPronunciation">{{ busy ? '处理中…' : '预览发音' }}</button></div>
      <div v-if="preview.length" class="result" aria-live="polite"><h3>逐音符音素预览</h3><ol><li v-for="(phones, index) in preview" :key="selected.notes[index]?.id">{{ selected.notes[index]?.lyric }} <code>{{ phones.join(' · ') }}</code></li></ol></div>
    </section>

    <section class="panel-section" aria-labelledby="override-title">
      <div class="section-heading"><h3 id="override-title">逐音符音素覆盖</h3></div>
      <label>自定义音素
        <textarea v-model="override" rows="2" :disabled="!selected.notes.length" placeholder="每组输入以空格分隔的音素；不同音符组之间用 | 分隔。" />
      </label>
      <p class="hint">为每个选中音符输入一组音素，并用 | 分隔各组。留空时将应用上方的预览结果。</p>
      <div class="actions"><button class="primary" :disabled="busy || !selected.notes.length || (!preview.length && !override.trim())" @click="applyPronunciation">应用到选中音符</button></div>
    </section>
    <p v-if="message" class="success" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.pronunciation{display:grid;gap:16px;width:min(100%,960px);margin:0 auto;color:#dce4f2;font:13px/1.5 system-ui,sans-serif;min-width:0}
header h2,.panel-section h3{margin:0;font-size:15px}header p,.hint{margin:4px 0 0;color:#929baa}
.language,.panel-section label{display:grid;gap:6px;color:#c8ced8;min-width:0}
.pronunciation select,.pronunciation textarea{box-sizing:border-box;min-width:0;width:100%;border:1px solid #3a4351;border-radius:5px;background:#12161d;color:#e5e7eb;padding:9px 10px;font:inherit}
.pronunciation select{height:36px}.pronunciation textarea{resize:vertical;max-height:180px;line-height:1.5}.pronunciation textarea::placeholder{color:#747f8e}.pronunciation textarea:focus,.pronunciation select:focus{outline:2px solid #8b70e8;outline-offset:1px}.pronunciation textarea:disabled{opacity:.55}
.panel-section{display:grid;gap:12px;padding:18px;border:1px solid #303742;border-radius:8px;background:#1c2027;min-width:0}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.section-heading span{color:#929baa;font-size:12px}
.actions{display:flex;justify-content:flex-end;flex-wrap:wrap}.pronunciation button{min-height:36px;border:1px solid #414b5b;border-radius:5px;background:#2a3240;color:inherit;padding:7px 14px;font:inherit;cursor:pointer}.pronunciation button:hover:not(:disabled){background:#354157}.pronunciation button:disabled{opacity:.45;cursor:default}.pronunciation .primary{background:#31445a;border-color:#465d77}
.result{padding:12px;border:1px solid #303742;border-radius:6px;background:#171b22;min-width:0}.result h3{margin:0;font-size:13px}.result ol{display:grid;gap:8px;margin:10px 0 0;padding-left:22px}.result li{padding-left:2px}.result code{display:block;margin-top:4px;overflow-wrap:anywhere;color:#c8d8e8}.success{color:#83cf9a}.error{color:#ed9292}.success,.error,.hint{overflow-wrap:anywhere}
@media(max-width:600px){.panel-section{padding:14px}}
</style>
