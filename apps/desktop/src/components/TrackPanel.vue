<script setup>
import { ref } from 'vue'
import { TRACK_ROW_H, TRACKS_HEAD_H } from '../stores/layout.js'
import { store, actions } from '../stores/project.js'
import { uiConfirm } from '../stores/ui.js'
import { trackRowH } from '../stores/layout.js'

const kinds = [
  { k: 'sing', label: '歌声轨' },
  { k: 'instrument', label: '乐器轨' },
  { k: 'audio', label: '音频轨' },
  { k: 'chord', label: '和弦轨' },
]

function selectTrack(t) {
  store.selection.trackId = t.id
}

async function saveTrackMix(trackId, property, value) {
  try {
    await actions.saveTrackMix(trackId, { [property]: value })
  } catch (error) {
    store.status = `混音保存失败：${error.message}`
  }
}
async function addTrack(k) {
  const name = kinds.find(x => x.k === k)?.label || k
  await actions.api('POST', '/track', { kind: k, name: `${name}${store.project.tracks.length + 1}` })
  await actions.refresh()
}
async function removeTrack(t) {
  if (!(await uiConfirm({ title: '删除轨道', message: `确定删除「${t.name}」？该操作可 Ctrl+Z 撤销。` }))) return
  await actions.api('DELETE', `/track/${t.id}`)
  await actions.refresh()
}
async function toggleMute(t) {
  await actions.api('PATCH', `/track/${t.id}`, { mute: !t.mute })
  await actions.refresh()
}
async function toggleSolo(t) {
  await actions.api('PATCH', `/track/${t.id}`, { solo: !t.solo })
  await actions.refresh()
}
</script>

<script>
export default { name: 'TrackPanel' }
</script>

<template>
  <div class="panel">
    <div v-for="t in store.project.tracks" :key="t.id" class="track"
      :style="{ height: trackRowH(t) + 'px' }"
      :class="{ sel: store.selection.trackId === t.id, mute: t.mute }" @click="selectTrack(t)">
      <div class="stripe" :style="{ background: t.colour || '#3a4048' }" />
      <div class="row1">
        <span class="kind" :data-k="t.kind">{{ t.kind === 'sing' ? '🎤' : t.kind === 'instrument' ? '🎹' : t.kind === 'audio' ? '🎵' : '🎼' }}</span>
        <span class="name">{{ t.name }}</span>
        <button class="mini" @click.stop="toggleMute(t)" :class="{ on: t.mute }">M</button>
        <button class="mini" @click.stop="toggleSolo(t)" :class="{ on: t.solo }">S</button>
        <button class="mini danger" @click.stop="removeTrack(t)">×</button>
      </div>
      <div class="row2">
        <span>{{ t.notes?.length || 0 }} 音符 · {{ t.singer_id === 'diffsinger.qixuan' ? '绮萱·真人声' : t.singer_id === 'builtin.fallback' ? '合成人声' : t.singer_id || t.instrument || '—' }}</span>
        <label title="轨道音量">V <input type="range" min="0" max="1" step="0.01" :value="t.volume ?? 1"
          @input="actions.updateTrackMix(t.id, 'volume', Number($event.target.value))"
          @change="saveTrackMix(t.id, 'volume', Number($event.target.value))" /></label>
        <label title="声像">P <input type="range" min="-1" max="1" step="0.01" :value="t.pan ?? 0"
          @input="actions.updateTrackMix(t.id, 'pan', Number($event.target.value))"
          @change="saveTrackMix(t.id, 'pan', Number($event.target.value))" /></label>
      </div>
    </div>
    <div v-if="!store.project.tracks.length" class="empty">点击上方 + 添加轨道</div>
  </div>
</template>

<style scoped>
.panel { display: flex; flex-direction: column; background: #181b20; overflow-y: auto; }
.head { height: 40px; /* TRACKS_HEAD_H */ box-sizing: border-box; display: flex; justify-content: space-between; align-items: center; padding: 8px 10px; font-size: 13px; color: #9aa3ad; }
.add button { background: #262b33; border: 1px solid #343a44; color: #9aa3ad; border-radius: 3px; cursor: pointer; margin-left: 3px; padding: 2px 7px; }
.track { box-sizing: border-box; padding: 7px 10px; border-bottom: 1px solid #22262d; cursor: pointer; position: relative; padding-left: 14px; }
.stripe { position: absolute; left: 4px; top: 6px; bottom: 6px; width: 4px; border-radius: 2px; opacity: 0.9; }
.track.sel { background: #232a35; }
.track.mute { opacity: 0.5; }
.row1 { display: flex; align-items: center; gap: 6px; }
.name { flex: 1; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.kind { font-size: 12px; }
.mini { background: #262b33; border: 1px solid #343a44; color: #9aa3ad; border-radius: 3px; font-size: 11px; padding: 1px 5px; cursor: pointer; }
.mini.on { background: #7c5cd6; color: white; border-color: #7c5cd6; }
.mini.danger:hover { background: #7f1d1d; color: white; }
 .row2 { display: flex; align-items: center; gap: 4px; font-size: 10px; color: #6b7280; margin-top: 3px; margin-left: 22px; white-space: nowrap; }
.row2 > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.row2 label { display: inline-flex; align-items: center; gap: 2px; flex: none; color: #78818d; }
.row2 input { width: 32px; height: 10px; margin: 0; accent-color: #7c5cd6; }
.empty { padding: 20px; text-align: center; color: #4b5563; font-size: 12px; }
</style>
