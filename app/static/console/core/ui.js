// 全局浮层：toast 与确认框。由外壳组件渲染，页面只调函数。
const { reactive } = Vue;

export const ui = reactive({ toasts: [], modal: null });

let seq = 0;

/** type: info | ok | warn | bad；action: { label, fn } */
export function toast(msg, type = 'info', action = null, ms = 4200) {
  const id = ++seq;
  ui.toasts.push({ id, msg, type, action });
  setTimeout(() => dropToast(id), ms);
}

export function dropToast(id) {
  const i = ui.toasts.findIndex((t) => t.id === id);
  if (i >= 0) ui.toasts.splice(i, 1);
}

/** 一次性操作失败的统一提示：「<做什么>失败：<后端原因>」。 */
export function toastError(what, e) {
  toast(`${what}失败：${e.message}`, 'bad', null, 6000);
}

export function confirmBox({ title, body, okText = '确定', cancelText = '取消' }) {
  return new Promise((resolve) => {
    const close = (v) => { ui.modal = null; resolve(v); };
    ui.modal = { title, body, okText, cancelText, ok: () => close(true), cancel: () => close(false) };
  });
}
