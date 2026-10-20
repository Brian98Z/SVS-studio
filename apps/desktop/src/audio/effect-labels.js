import zh from './effect-labels.zh-CN.json'

export function effectLabel(type) { return zh.effects[type] || type }
export function parameterLabel(name) {
  if (zh.parameters[name]) return zh.parameters[name]
  const band = /^band_(\d+)_(.+)$/.exec(name)
  if (band) return `频段 ${band[1]} · ${zh.fields[band[2]] || band[2]}`
  const group = /^(wide_band|hp|lp)_(.+)$/.exec(name)
  if (group) return `${zh.groups[group[1]]} · ${zh.fields[group[2]] || group[2]}`
  return name
}
export function parameterValueLabel(value) {
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if (typeof value !== 'string') return String(value)
  if (zh.values[value]) return zh.values[value]
  if (/^USER\d+$/.test(value)) return `用户预设 ${value.slice(4)}`
  return value.replace('dB/oct', 'dB/倍频程').replace('-/+ ', '±')
}
