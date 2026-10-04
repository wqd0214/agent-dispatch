// 作业详情与日志增量轮询
import { getCurrentInstance, onUnmounted, ref, watch, type Ref } from 'vue'
import { api } from '../api'
import { appendLog, type LogState } from '../lib/log'
import { usePolling } from './usePolling'

export function useJobDetail(name: Ref<string>) {
  return usePolling(() => api.jobDetail(name.value), 3000)
}

export function useJobLog(name: Ref<string>, running: Ref<boolean>) {
  const state = ref<LogState>({ offset: -1, text: '' }) // -1：首次由服务端从尾部补
  const offline = ref(false)
  let timer: number | undefined
  let disposed = false

  async function pull() {
    if (disposed) return
    try {
      const chunk = await api.jobLog(name.value, state.value.offset)
      state.value = appendLog(state.value, chunk)
      offline.value = false
    } catch {
      offline.value = true
    }
    if (!disposed) timer = window.setTimeout(pull, 3000)
  }

  watch(name, n => {
    if (n) state.value = { offset: -1, text: '' }
  })
  watch(running, r => {
    if (!r) void pull() // 结束后再拉一次收尾
  })
  void pull()
  if (getCurrentInstance()) onUnmounted(() => { disposed = true; if (timer != null) window.clearTimeout(timer) })
  return { state, offline }
}
