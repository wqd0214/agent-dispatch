<script setup lang="ts">
// 主管规则页：编辑并保存当前项目的 supervisor.md，旁边只读显示本项目可派的角色清单。
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { configApi, projectApi } from '../api'
import type { ConfigOverview } from '../types'
import { useCurrentProject } from '../composables/useProject'
import { inheritLabel } from '../lib/roles'
import DefineProject from '../components/DefineProject.vue'

const { selected: projectId } = useCurrentProject()
const config = ref<ConfigOverview | null>(null)
const text = ref('')
const loading = ref(false)
const saving = ref(false)
const origin = ref('')

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }
async function reload() {
  loading.value = true
  try {
    config.value = await configApi.overview()
    text.value = project.value?.supervisor ?? ''
    origin.value = text.value
  } catch (e) { ElMessage.error(msg(e)) } finally { loading.value = false }
}
const project = computed(() => config.value?.projects.find(p => p.id === projectId.value) ?? null)
const dirty = computed(() => !!project.value && text.value !== origin.value)
watch(projectId, reload)
void reload()

async function save() {
  if (!project.value) return
  saving.value = true
  try {
    await projectApi.saveSupervisor(project.value.id, text.value)
    origin.value = text.value
    ElMessage.success('已保存，下次派工发给主管时生效')
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
</script>

<template>
  <div class="sup" v-loading="loading">
    <el-empty v-if="!projectId" description="先在左上角选择一个项目，再编辑它的主管规则" />
    <template v-else-if="!project">
      <DefineProject :project-id="projectId"
        text="这个项目还没有在 agent-dispatch 里定义；定义之后才能编辑主管规则" @defined="reload" />
    </template>
    <div v-else class="grid">
      <div class="editor">
        <h2>主管规则（supervisor.md）</h2>
        <el-input v-model="text" type="textarea" :rows="22" class="mono" placeholder="写给主管的固定说明，每次派工都会附带" />
        <div class="btns">
          <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">保存主管规则</el-button>
          <span class="sub">保存到项目的 supervisor.md，原样下发给主管</span>
        </div>
      </div>
      <div class="roles">
        <h2>本项目可派的角色</h2>
        <div v-for="r in project.roles" :key="r.id" class="role">
          <div class="t">{{ r.title }} <el-tag v-if="inheritLabel(r.scope, true)" size="small" type="info">{{ inheritLabel(r.scope, true) }}</el-tag></div>
          <div class="sub mono">{{ r.id }}</div>
          <div v-if="r.description" class="sub">{{ r.description }}</div>
        </div>
        <div v-if="!project.roles.length" class="sub">还没有角色</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sup { padding: 2px; overflow: auto; }
.grid { display: grid; grid-template-columns: 1fr 280px; gap: 14px; }
h2 { margin: 6px 0 8px; font-size: 15px; }
.btns { margin-top: 8px; display: flex; align-items: center; gap: 10px; }
.roles { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; }
.role { border-bottom: 1px solid var(--el-border-color-lighter); padding: 6px 2px; }
.role:last-of-type { border-bottom: none; }
.role .t { font-weight: 600; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
