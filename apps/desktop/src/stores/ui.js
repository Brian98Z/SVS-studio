// 应用内对话框的 Promise 化入口。
// App.vue 挂载 <AppModal>；resolve 后关闭。
import { reactive } from 'vue'

export const ui = reactive({
  dialog: null, // {title,label,placeholder,initial,mode,options,message,resolve}
})

function open(cfg) {
  return new Promise((resolve) => {
    ui.dialog = { ...cfg, resolve }
  })
}

export function uiPrompt(cfg) { return open({ ...cfg, mode: 'prompt' }) }
export function uiConfirm(cfg) { return open({ ...cfg, mode: 'confirm' }) }
export function uiSelect(cfg) { return open({ ...cfg, mode: 'select' }) }

/** AppModal @done 处理：resolve 并关闭。 */
export function uiDone(v) {
  if (!ui.dialog) return
  const r = ui.dialog.resolve
  ui.dialog = null
  r(v)
}
