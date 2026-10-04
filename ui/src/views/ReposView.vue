<script setup lang="ts">
// 工程页：当前项目的工程列表（增删改）、项目显示名与默认角色；未定义的项目给「定义为项目」入口。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { configApi, projectApi } from '../api'
import type { ConfigOverview } from '../types'
import { useCurrentProject } from '../composables/useProject'
import DefineProject from '../components/DefineProject.vue'

const { selected: projectId } = useCurrentProject()
const config = ref<ConfigOverview | null>(null)
const loading = ref(false)
const saving = ref(false)

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }
async function reload() {
  loading.value = true
  try { config.value = await configApi.overview() } catch (e) { ElMessage.error(msg(e)) } finally { loading.value = false }
}
void reload()
watch(projectId, () => { syncMeta(); void reload() })

const project = computed(() => config.value?.projects.find(p => p.id === projectId.value) ?? null)
const roleOptions = computed(() => project.value?.roles ?? [])

// 被哪些角色引用：合并后的角色里 repos 包含该工程的都算
function referencingRoles(repoId: string): string {
  const roles = (project.value?.roles ?? []).filter(r => (r.repos ?? []).includes(repoId))
  return roles.map(r => r.title).join('、')
}

// ---------- 项目显示名 / 默认角色 ----------
const meta = reactive({ name: '', default_role: '' })
function syncMeta() {
  meta.name = project.value?.name ?? ''
  meta.default_role = project.value?.default_role ?? ''
}
watch(project, syncMeta)
syncMeta()
const metaDirty = computed(() =>
  project.value && (meta.name !== project.value.name || meta.default_role !== (project.value.default_role ?? '')))
async function saveMeta() {
  if (!project.value) return
  saving.value = true
  try {
    await projectApi.update(project.value.id, {
      ...(meta.name !== project.value.name ? { name: meta.name } : {}),
      ...(meta.default_role !== (project.value.default_role ?? '') ? { default_role: meta.default_role } : {}),
    })
    await reload(); ElMessage.success('已保存')
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}

// ---------- 工程新增 / 修改 / 删除 ----------
const editOpen = ref(false)
const editSaving = ref(false)
const form = reactive({ id: '', cwd: '', channelsText: '', channelNs: '' })
let editing = false
function openCreate() {
  editing = false
  Object.assign(form, { id: '', cwd: '', channelsText: '', channelNs: '' })
  editOpen.value = true
}
function openEdit(repo: ConfigOverview['projects'][number]['repos'][number]) {
  editing = true
  Object.assign(form, { id: repo.id, cwd: repo.cwd, channelsText: (repo.channels ?? []).join(', '), channelNs: repo.channel_ns ?? '' })
  editOpen.value = true
}
async function saveRepo() {
  if (!project.value) return
  editSaving.value = true
  try {
    await projectApi.addRepo(project.value.id, {
      id: form.id.trim(), cwd: form.cwd.trim(),
      channels: form.channelsText.split(',').map(c => c.trim()).filter(Boolean),
      channel_namespace: form.channelNs.trim() || undefined,
    })
    ElMessage.success(editing ? '已保存' : '已添加')
    editOpen.value = false
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { editSaving.value = false }
}
async function removeRepo(repo: ConfigOverview['projects'][number]['repos'][number]) {
  if (!project.value) return
  const { ElMessageBox } = await import('element-plus')
  try {
    await ElMessageBox.confirm(`从项目 ${project.value.id} 删除工程 ${repo.id}？`, '二次确认', { type: 'warning' })
  } catch { return }
  try {
    await projectApi.removeRepo(project.value.id, repo.id)
    ElMessage.success('已删除')
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } // 被角色引用等服务端拒绝原因原样展示
}
</script>

<template>
  <div class="repos" v-loading="loading">
    <el-empty v-if="!projectId" description="先在左上角选择一个项目，再管理它的工程" />
    <template v-else-if="!project">
      <DefineProject :project-id="projectId"
        text="这个项目还没有在 agent-dispatch 里定义；定义之后才能管理它的工程" @defined="reload" />
      <el-empty description="定义项目之后这里会出现工程列表" :image-size="80" />
    </template>
    <template v-else>
      <h2>项目设置</h2>
      <el-form label-width="110px" inline class="form">
        <el-form-item label="显示名">
          <el-input v-model="meta.name" style="width: 220px" />
        </el-form-item>
        <el-form-item label="默认角色">
          <el-select v-model="meta.default_role" clearable style="width: 200px" placeholder="（无）">
            <el-option v-for="r in roleOptions" :key="r.id" :value="r.id" :label="r.title" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :disabled="!metaDirty" :loading="saving" @click="saveMeta">保存项目设置</el-button>
        </el-form-item>
      </el-form>

      <h2>工程列表</h2>
      <div class="toolbar">
        <el-button size="small" type="primary" @click="openCreate">新增工程</el-button>
      </div>
      <el-table :data="project.repos" size="small" class="tbl">
        <el-table-column prop="id" label="工程 ID" min-width="120" />
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="cwd" label="目录" min-width="260">
          <template #default="{ row }"><span class="mono">{{ row.cwd }}</span></template>
        </el-table-column>
        <el-table-column label="通道" min-width="140">
          <template #default="{ row }">{{ (row.channels ?? []).join('、') || '-' }}</template>
        </el-table-column>
        <el-table-column prop="channel_ns" label="通道命名空间" min-width="140" />
        <el-table-column label="被哪些角色引用" min-width="160">
          <template #default="{ row }">{{ referencingRoles(row.id) || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-button size="small" link type="primary" @click="openEdit(row)">修改</el-button>
            <el-button size="small" link type="danger" @click="removeRepo(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <el-dialog v-model="editOpen" :title="editing ? '修改工程' : '新增工程'" width="520px">
      <el-form label-width="110px">
        <el-form-item label="工程 ID" required>
          <el-input v-model="form.id" :disabled="editing" placeholder="如 client" />
        </el-form-item>
        <el-form-item label="目录" required>
          <el-input v-model="form.cwd" class="mono" placeholder="工程的本地目录" />
        </el-form-item>
        <el-form-item label="通道">
          <el-input v-model="form.channelsText" placeholder="逗号分隔，可留空" />
        </el-form-item>
        <el-form-item label="通道命名空间">
          <el-input v-model="form.channelNs" placeholder="缺省为 <项目>.<工程>" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">取消</el-button>
        <el-button type="primary" :loading="editSaving" :disabled="!form.id.trim() || !form.cwd.trim()" @click="saveRepo">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.repos { padding: 2px; overflow: auto; }
h2 { margin: 14px 0 8px; font-size: 15px; }
.toolbar { margin-bottom: 8px; }
.tbl { width: 100%; }
.form :deep(.el-form-item__content) { flex-wrap: wrap; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
</style>
