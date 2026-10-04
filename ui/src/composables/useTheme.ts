// 主题：跟随系统 / 亮色 / 暗色，记在 localStorage；Element Plus 暗色走 html.dark
import { computed, ref, watchEffect } from 'vue'

export type ThemeMode = 'system' | 'light' | 'dark'
const KEY = 'agent-dispatch-theme'
const DARK_QUERY = window.matchMedia('(prefers-color-scheme: dark)')

function stored(): ThemeMode {
  const fromUrl = new URLSearchParams(location.search).get('theme')
  if (fromUrl === 'light' || fromUrl === 'dark') return fromUrl // URL 可强制主题（便于客户端 / 截图）
  const v = localStorage.getItem(KEY)
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system'
}

export function useTheme() {
  const mode = ref<ThemeMode>(stored())
  const systemDark = ref(DARK_QUERY.matches)
  DARK_QUERY.addEventListener('change', e => { systemDark.value = e.matches })
  const isDark = computed(() => mode.value === 'dark' || (mode.value === 'system' && systemDark.value))

  watchEffect(() => {
    document.documentElement.classList.toggle('dark', isDark.value)
    localStorage.setItem(KEY, mode.value)
  })

  return { mode, isDark }
}
