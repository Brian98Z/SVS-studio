// 布局常量：左侧轨道行高 ↔ 右侧泳道行高（两侧对齐的关键）。
// 人声轨加宽（76px）便于歌词显示；其余轨 52px。
export const TRACKS_HEAD_H = 40
export const ROW_H = 52
export const SING_ROW_H = 76

/** 按轨道类型取行高。track 可为 undefined（按普通轨处理）。 */
export function trackRowH(track) {
  return track && track.kind === 'sing' ? SING_ROW_H : ROW_H
}

/** 前缀兼容：旧行高常量（普通轨）。 */
export const TRACK_ROW_H = ROW_H
