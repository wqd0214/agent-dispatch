<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { useCurrentProject, useProjectScope } from '../composables/useProject'
import { useTheme } from '../composables/useTheme'

const route = useRoute()
const { mode } = useTheme()
const { selected, set } = useCurrentProject()
const o = useSharedOverview()
const { entries, jobs, currentName } = useProjectScope(() => o.data.value)
const connected = computed(() => !o.offline.value)

const activeNav = computed(() => (route.path.startsWith('/jobs') ? 'jobs' : route.path))
</script>

<template>
  <el-container class="shell">
    <aside class="side">
      <div class="brand">agent-dispatch</div>
      <el-select :model-value="selected" class="proj" size="small" @update:model-value="set">
        <el-option v-for="e in entries" :key="e.id || '__all__'" :value="e.id" :label="e.label">
          <span>{{ e.label }}</span>
          <el-badge v-if="e.pending" :value="e.pending" class="proj-badge" />
        </el-option>
      </el-select>
      <nav class="nav">
        <router-link v-for="item in [
          { key: '/dashboard', label: '工作台', badge: jobs.filter(j => j.needs_user || ['done', 'failed', 'interrupted'].includes(j.state)).length },
          { key: '/jobs', label: '作业', badge: 0 },
          { key: '/batches', label: '批次', badge: 0 },
          { key: '/stats', label: '统计', badge: 0 },
        ]" :key="item.key" :to="item.key" class="nav-item" :class="{ on: activeNav === item.key }">
          <span>{{ item.label }}</span>
          <span v-if="item.badge" class="badge">{{ item.badge }}</span>
        </router-link>
      </nav>
      <div class="sec">项目设置</div>
      <nav class="nav">
        <router-link to="/settings/projects" class="nav-item" :class="{ on: activeNav === '/settings/projects' }">项目设置</router-link>
      </nav>
      <div class="sec">全局</div>
      <nav class="nav">
        <a href="/legacy#/models" class="nav-item">派工工具与模型</a>
      </nav>
    </aside>
    <el-container>
      <header class="topbar">
        <div class="title">{{ currentName }}</div>
        <div class="spacer" />
        <el-button type="primary" size="small" disabled title="派工面板在下一个版本提供">派工</el-button>
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
      <el-main class="main">
        <el-alert v-if="!connected" type="error" :closable="false" show-icon
          title="无法连接 agent-dispatch 服务，正在自动重试……恢复后界面会自动继续刷新。" class="offline" />
        <slot />
      </el-main>
    </el-container>
  </el-container>
</template>

<script lang="ts">
export default { name: 'AppShell' }
</script>

<style scoped>
.shell { height: 100%; }
.side {
  width: 216px; flex: none; display: flex; flex-direction: column;
  background: #1e232b; color: #c9d1d9; padding: 14px 10px;
}
html.dark .side { background: #0b0e13; }
.brand { font-weight: 600; padding: 0 10px 10px; color: #fff; letter-spacing: .02em; }
.proj { padding: 0 4px 14px; }
.proj-badge { margin-left: 8px; }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item {
  display: flex; justify-content: space-between; align-items: center;
  padding: 6px 10px; border-radius: 6px; color: #c9d1d9;
}
.nav-item:hover { background: rgba(255, 255, 255, .08); }
.nav-item.on { background: rgba(64, 158, 255, .25); color: #fff; }
.badge { background: #8250df; color: #fff; border-radius: 10px; padding: 0 7px; font-size: 11px; }
.sec { color: #8b949e; font-size: 11px; margin: 14px 10px 4px; letter-spacing: .05em; }
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
.main { padding: 14px 16px; overflow: auto; }
.offline { margin-bottom: 10px; }
</style>
