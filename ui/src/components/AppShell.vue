<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { useCurrentProject, useProjectScope } from '../composables/useProject'
import { useTheme } from '../composables/useTheme'
import DispatchPanel from './DispatchPanel.vue'
import JobSearch from './JobSearch.vue'

const route = useRoute()
const router = useRouter()
const { mode } = useTheme()
const { selected, set } = useCurrentProject()
const o = useSharedOverview()
const { entries, jobs, currentName } = useProjectScope(() => o.data.value)
const connected = computed(() => !o.offline.value)
// 服务进程还是旧代码（磁盘版本已变）：顶部横幅提醒重启，不拦操作
const RESTART_CMD = 'agent-dispatch web stop && agent-dispatch web start'
const stale = computed(() => {
  const v = o.data.value?.versions
  return !!v && v.process !== v.disk
})
async function copyRestart() {
  try { await navigator.clipboard.writeText(RESTART_CMD) } catch { /* 剪贴板不可用就忽略，命令文字就在旁边 */ }
}

const activeNav = computed(() => {
  if (route.path === '/' || route.path.startsWith('/dashboard')) return '/dashboard'
  return route.path.startsWith('/jobs') ? '/jobs' : route.path
})
const pending = computed(() => jobs.value.filter(j => j.needs_user || ['done', 'failed', 'interrupted'].includes(j.state)).length)
const settingPages = [
  { path: '/settings/repos', label: '工程' },
  { path: '/settings/roles', label: '角色' },
  { path: '/settings/mcp', label: 'MCP' },
  { path: '/settings/skills', label: '技能' },
  { path: '/settings/supervisor', label: '主管规则' },
]

// 快捷键：⌘/Ctrl+N 派工、⌘/Ctrl+K 搜索、⌘/Ctrl+1~4 切页
const dispatchOpen = ref(false)
const searchOpen = ref(false)
const navByNumber = ['/dashboard', '/jobs', '/batches', '/stats']
function onKeydown(e: KeyboardEvent) {
  if (!(e.metaKey || e.ctrlKey)) return
  const key = e.key.toLowerCase()
  if (key === 'n') {
    e.preventDefault()
    dispatchOpen.value = true
  } else if (key === 'k') {
    e.preventDefault()
    searchOpen.value = true
  } else if (['1', '2', '3', '4'].includes(e.key)) {
    e.preventDefault()
    void router.push(navByNumber[Number(e.key) - 1])
  }
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="shell">
    <aside class="side">
      <div class="brand">agent-dispatch</div>
      <el-select :model-value="selected" class="proj" size="small" placeholder="全部项目" @update:model-value="set">
        <el-option v-for="e in entries" :key="e.id || '__all__'" :value="e.id" :label="e.label">
          <span>{{ e.label }}</span>
          <span v-if="e.pending" class="proj-badge">{{ e.pending }}</span>
        </el-option>
      </el-select>
      <nav class="nav">
        <router-link v-for="item in [
          { key: '/dashboard', label: '工作台' },
          { key: '/jobs', label: '作业' },
          { key: '/batches', label: '事项' },
          { key: '/stats', label: '统计' },
        ]" :key="item.key" :to="item.key" class="nav-item" :class="{ on: activeNav === item.key }">
          <span>{{ item.label }}</span>
          <span v-if="item.key === '/dashboard' && pending" class="badge">{{ pending }}</span>
        </router-link>
      </nav>
      <div class="sec">项目设置</div>
      <nav class="nav">
        <router-link v-for="p in settingPages" :key="p.path" :to="p.path" class="nav-item" :class="{ on: activeNav === p.path }">
          {{ p.label }}
        </router-link>
      </nav>
      <div class="sec">全局</div>
      <nav class="nav">
        <router-link to="/global/models" class="nav-item" :class="{ on: activeNav === '/global/models' }">工具与模型</router-link>
      </nav>
    </aside>
    <div class="right">
      <header class="topbar">
        <div class="title">{{ currentName }}</div>
        <div class="spacer" />
        <el-button type="primary" size="small" @click="dispatchOpen = true">派工</el-button>
        <el-dropdown @command="(m: string) => (mode = m as typeof mode)">
          <el-button size="small">
            {{ mode === 'system' ? '跟随系统' : mode === 'light' ? '亮色' : '暗色' }}
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="system">跟随系统</el-dropdown-item>
              <el-dropdown-item command="light">亮色</el-dropdown-item>
              <el-dropdown-item command="dark">暗色</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <span class="conn" :class="connected ? 'ok' : 'bad'">
          <span class="dot" />{{ connected ? '已连接' : '连接断开' }}
        </span>
      </header>
      <main class="main">
        <el-alert v-if="!connected" type="error" :closable="false" show-icon
          title="无法连接 agent-dispatch 服务，正在自动重试……恢复后界面会自动继续刷新。" class="offline" />
        <el-alert v-if="stale" type="warning" :closable="false" show-icon class="offline">
          <template #title>
            网页服务还在运行旧版本，部分功能不可用。请在终端执行：{{ RESTART_CMD }}
            <el-button size="small" type="primary" style="margin-left: 10px" @click="copyRestart">复制命令</el-button>
          </template>
        </el-alert>
        <slot />
      </main>
    </div>
    <DispatchPanel v-model="dispatchOpen" />
    <JobSearch v-model="searchOpen" />
  </div>
</template>

<style scoped>
.shell { display: grid; grid-template-columns: 210px 1fr; height: 100vh; }
.side {
  display: flex; flex-direction: column;
  background: #1e232b; color: #c9d1d9; padding: 14px 10px; overflow-y: auto;
}
html.dark .side { background: #0b0e13; }
.brand { font-weight: 600; padding: 0 10px 10px; color: #fff; letter-spacing: .02em; }
.proj { padding: 0 4px 14px; }
.proj-badge { margin-left: 8px; background: #8250df; color: #fff; border-radius: 10px; padding: 0 7px; font-size: 11px; }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 10px; border-radius: 6px; color: #c9d1d9;
}
.nav-item:hover { background: rgba(255, 255, 255, .08); }
.nav-item.on { background: rgba(64, 158, 255, .25); color: #fff; }
.badge { background: #8250df; color: #fff; border-radius: 10px; padding: 0 7px; font-size: 11px; }
.sec { color: #8b949e; font-size: 11px; margin: 14px 10px 4px; letter-spacing: .05em; }
.right { display: flex; flex-direction: column; min-width: 0; height: 100vh; }
.topbar {
  height: 48px; flex: none; display: flex; align-items: center; gap: 10px;
  border-bottom: 1px solid var(--el-border-color); padding: 0 16px;
  background: var(--el-bg-color);
}
.title { font-weight: 600; }
.spacer { flex: 1; }
.conn { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--el-color-danger); }
.conn.ok .dot { background: var(--el-color-success); }
.main { flex: 1; min-height: 0; overflow: auto; padding: 14px 16px; }
.offline { margin-bottom: 10px; }
</style>
