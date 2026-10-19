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
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}

async function applyPronunciation() {
  const notes = selected.value.notes
  if (!notes.length) return
const groups = override.value.trim()
    ? override.value.split(/\s*\|\s*/).map(group => group.trim().split(/\s+/).filter(Boolean))
    : preview.value
  if (groups.length !== notes.length) {
    error.value = `Expected ${notes.length} per-note groups; use | between explicit note groups.`
    return
  }
  busy.value = true; error.value = ''; message.value = ''
  try {
    for (const [index, note] of notes.entries()) {
      await actions.api('PATCH', `/track/${encodeURIComponent(selected.value.track.id)}/note/${encodeURIComponent(note.id)}`, { phonemes: groups[index] })
      note.phonemes = groups[index]
    }
    message.value = 'Pronunciation saved.'
  } catch (cause) { error.value = cause.message }
  finally { busy.value = false }
}
</script>

<template>
  <section class="pronunciation" aria-labelledby="pronunciation-title">
    <header><div><h2 id="pronunciation-title">Pronunciation</h2><p>Preview language-aware phonemes and save an explicit note override.</p></div></header>
    <label>Language
      <select v-model="language">
        <option value="zh">Mandarin</option><option value="yue">Cantonese</option>
        <option value="en">English</option><option value="ja">Japanese</option><option value="ko">Korean</option>
      </select>
    </label>
    <label>Selected lyric phrase
      <textarea :value="lyric" readonly rows="3" placeholder="Select one or more notes in the timeline." />
    </label>
    <div class="actions"><button :disabled="busy || !lyric" @click="previewPronunciation">{{ busy ? 'Working…' : 'Preview' }}</button></div>
    <section v-if="preview.length" class="result" aria-live="polite"><h3>Per-note phonemes</h3><ol><li v-for="(phones, index) in preview" :key="selected.notes[index]?.id">{{ selected.notes[index]?.lyric }} <code>{{ phones.join(' · ') }}</code></li></ol></section>
    <label>Explicit phoneme override
      <textarea v-model="override" rows="2" placeholder="Optional space-separated phonemes" />
    </label>
    <p class="hint">Enter one phoneme group per note, separated by |. Preview uses the whole lyric phrase for context.</p>
    <button class="primary" :disabled="busy || !selected.notes.length || (!preview.length && !override.trim())" @click="applyPronunciation">Apply to selected notes</button>
    <p v-if="message" class="success" role="status">{{ message }}</p><p v-if="error" class="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.pronunciation{display:grid;gap:16px;max-width:680px}.pronunciation h2,.pronunciation h3{margin:0;font-size:15px}.pronunciation header p,.hint{margin:4px 0 0;color:#929baa}.pronunciation label{display:grid;gap:6px;color:#c8ced8}.pronunciation select,.pronunciation textarea{box-sizing:border-box;width:100%;border:1px solid #363d48;border-radius:6px;background:#11141a;color:#e5e7eb;padding:9px 10px;font:inherit}.pronunciation textarea{resize:vertical}.actions{display:flex;justify-content:flex-end}.pronunciation button{border:1px solid #3a414d;border-radius:6px;background:#222730;color:#e5e7eb;padding:8px 12px;cursor:pointer}.pronunciation button:disabled{opacity:.5;cursor:default}.pronunciation .primary{justify-self:start;background:#31445a;border-color:#465d77}.result{padding:12px;border:1px solid #303742;border-radius:6px;background:#1d222a}.result code{display:block;margin-top:8px;overflow-wrap:anywhere;color:#c8d8e8}.success{color:#83cf9a}.error{color:#ed9292}
</style>
