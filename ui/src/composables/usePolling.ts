// 通用轮询：带连接状态与恢复。fetch 失败标记 offline，成功即恢复并立即回调。
import { computed, getCurrentInstance, onUnmounted, ref, type Ref } from 'vue'
import { api } from '../api'
import type { Overview } from '../types'

export interface PollingHandle<T> { data: Ref<T | null>; offline: Ref<boolean>; error: Ref<string>; refresh: () => void }

export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number, initial: T | null = null): PollingHandle<T> {
  const data = ref(initial) as Ref<T | null>
  const offline = ref(false)
  const error = ref('')
  let timer: number | undefined
  let disposed = false

  async function tick() {
    if (disposed) return
    try {
      data.value = await fetcher()
      offline.value = false
      error.value = ''
    } catch (e) {
      offline.value = true
      error.value = e instanceof Error ? e.message : String(e)
    }
    if (!disposed) timer = window.setTimeout(tick, intervalMs)
  }

  function refresh() {
    if (timer != null) window.clearTimeout(timer)
    void tick()
  }

  void tick()
  if (getCurrentInstance()) onUnmounted(() => {
    disposed = true
    if (timer != null) window.clearTimeout(timer)
  })
  return { data, offline, error, refresh }
}

export function useOverview() {
  return usePolling(() => api.overview(), 3000)
}

// 全局共享的总览轮询（模块级单例）：侧栏和各页面用同一份数据与连接状态
let shared: PollingHandle<Overview> | null = null
export function useSharedOverview(): PollingHandle<Overview> {
  if (!shared) shared = useOverview()
  return shared
}
