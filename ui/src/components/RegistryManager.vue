<script setup lang="ts">
// MCP / 技能登记页共用。选中项目时按项目管理：现状一句话 + 管理开关、主表（已登记的
// 本项目 + 全局，和工程里发现未纳入的同一棵表）、按角色调整（默认折叠）；
// 全部项目视图只看全局登记与全局角色层。登记表增删改、导入对话框沿用原实现。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { configApi, mcpApi, projectApi, skillApi } from '../api'
import type { CandidateGroup, ConfigOverview, McpEntry, RoleDetail, SkillEntry } from '../types'
import { useCurrentProject } from '../composables/useProject'
import {
  DISPATCH_PENDING_NOTE, STATUS_LABEL, discoveredCandidateIds, duplicateNameError, filterCandidateGroups,
  mergeRegistryRows, mcpEntrySummary, selectableCandidateIds, toggleRegistryId,
} from '../lib/registry'
import type { RegistryRow } from '../lib/registry'
import { projectRoleRows, type RoleListRow } from '../lib/roles'
import DefineProject from './DefineProject.vue'

const props = defineProps<{ kind: 'mcp' | 'skills' }>()
const isMcp = computed(() => props.kind === 'mcp')
const { selected: projectId } = useCurrentProject()

const config = ref<ConfigOverview | null>(null)
const entries = ref<(McpEntry | SkillEntry)[]>([])
const managed = ref(false)
const enabled = ref<string[]>([])
const pageRoles = ref<{ id: string; title: string; managed: boolean; value: string[] | null; override: boolean; source: string | null }[]>([])
const roleDetails = ref<Record<string, RoleDetail>>({})
const candidates = ref<CandidateGroup[]>([])
const loading = ref(false)
const saving = ref(false)
const matrixKey = ref(0) // 保存被拒后强制重画对照表，勾选框回到真实值

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }
const kindLabel = computed(() => (isMcp.value ? 'MCP 服务' : '技能'))

async function reload() {
  loading.value = true
  try {
    const [cfg, page, cands] = await Promise.all([
      configApi.overview(),
      isMcp.value ? mcpApi.page(pageQuery.value) : skillApi.page(pageQuery.value),
      (isMcp.value ? mcpApi.candidates(candScope.value) : skillApi.candidates(candScope.value)),
    ])
    config.value = cfg
    entries.value = isMcp.value ? (page as { mcp: McpEntry[] }).mcp : (page as { skills: SkillEntry[] }).skills
    managed.value = page.managed
    enabled.value = page.enabled
    pageRoles.value = page.roles
    candidates.value = cands.candidates
    await reloadRoleDetails()
  } catch (e) { ElMessage.error(msg(e)) } finally { loading.value = false }
}

const scopeRows = computed(() => projectRoleRows(config.value, projectId.value))
const projectDefined = computed(() => scopeRows.value.defined)
const inProject = computed(() => !!projectId.value && projectDefined.value)
// 对照表和角色页一致：项目没定义或全部项目视图都按全局层读写
const scopeProject = computed(() => inProject.value ? projectId.value : '')
// 主表 / 候选的范围：项目视图 = 本项目，全局视图 = 只看全局
const pageQuery = computed(() => (inProject.value ? projectId.value : undefined))
const candScope = computed(() => (inProject.value ? projectId.value : ''))
const scopeLabel = computed(() => (inProject.value ? `本项目（${projectId.value}）` : '全局'))

const rows = computed<RegistryRow[]>(() =>
  mergeRegistryRows(entries.value as never[], candidates.value, { managed: managed.value, enabled: enabled.value, scope: inProject.value ? projectId.value : '' }))
const discoveredIds = computed(() => discoveredCandidateIds(candidates.value, candScope.value))

function ownerLabel(row: RegistryRow): string {
  if (inProject.value) return row.owner === projectId.value ? '本项目' : '全局'
  return '全局'
}
function rowSummary(row: RegistryRow): string {
  if (!isMcp.value) return row.path ?? ''
  return mcpEntrySummary(row.entry)
}
function statusTone(status: RegistryRow['status']): 'success' | 'info' | 'warning' {
  return status === 'enabled' ? 'success' : status === 'discovered' ? 'warning' : 'info'
}
const statusLine = computed(() => {
  if (!inProject.value) return null
  if (!managed.value) return `本项目未管理${kindLabel.value}：执行者照旧用工程里自己的配置`
  const n = enabled.value.length
  const overridden = pageRoles.value.filter(r => r.override)
  const extra = overridden.length ? `；${overridden.map(r => r.title).join('、')}另有单独配置` : ''
  return `本项目派工时带 ${n} 个${kindLabel.value}${extra}`
})

async function reloadRoleDetails() {
  const out: Record<string, RoleDetail> = {}
  for (const r of scopeRoles.value) {
    try { out[r.id] = await configApi.roleDetail(r.id, scopeProject.value) } catch { /* 角色细节读不到就当未管理 */ }
  }
  roleDetails.value = out
}
const scopeRoles = computed<RoleListRow[]>(() => scopeRows.value.rows)
watch(scopeProject, reloadRoleDetails)
watch(() => [projectId.value, props.kind], reload)
void reload()

function roleTitle(project: string, role: string): string {
  if (!config.value) return role
  if (!project) return config.value.roles.find(r => r.id === role)?.title ?? role
  return config.value.projects.find(p => p.id === project)?.roles.find(r => r.id === role)?.title ?? role
}

// ---------- 本项目的管理开关与启用清单（写项目 defaults） ----------
async function saveEnabled(list: string[]) {
  if (!inProject.value) return
  const dup = duplicateNameError(list, id => entries.value.find(e => e.id === id)?.name ?? id)
  if (dup) { ElMessage.error(`${kindLabel.value}里选了两个同名的：${dup}`); return }
  saving.value = true
  try {
    await projectApi.update(projectId.value, { defaults: { [props.kind]: list } })
    enabled.value = list
    managed.value = true
    await reloadRoleDetails()
    ElMessage.success('已保存，本项目派工时生效')
  } catch (e) { ElMessage.error(msg(e)); await reload() } finally { saving.value = false }
}
async function toggleManaged(on: boolean) {
  if (!inProject.value) return
  if (!on) {
    const { ElMessageBox } = await import('element-plus')
    try {
      await ElMessageBox.confirm(
        `关闭后本项目恢复「未管理」：派工时不再由 agent-dispatch 指定带哪些${kindLabel.value}，执行者照旧用自己的配置。`,
        '二次确认', { type: 'warning' })
    } catch { return }
    saving.value = true
    try {
      await projectApi.update(projectId.value, { defaults: { [props.kind]: null } })
      ElMessage.success('已恢复未管理：执行者照旧用它自己的配置')
      await reload()
    } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
    return
  }
  await saveEnabled([])
}
async function toggleEnabled(row: RegistryRow) {
  await saveEnabled(toggleRegistryId(enabled.value, row.id))
}
async function includeVariant(row: RegistryRow) {
  if (row.kind !== 'candidate' || !row.variant) return
  saving.value = true
  try {
    const r = isMcp.value
      ? await mcpApi.importIds([row.id], projectId.value || undefined)
      : await skillApi.importIds([row.id], projectId.value || undefined)
    ElMessage.success(r.added.length ? `已纳入：${r.added.join('、')}` : '这个已经登记过了')
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
async function includeAll() {
  if (!discoveredIds.value.length) return
  saving.value = true
  try {
    const r = isMcp.value
      ? await mcpApi.importIds(discoveredIds.value, projectId.value || undefined)
      : await skillApi.importIds(discoveredIds.value, projectId.value || undefined)
    ElMessage.success(`已纳入 ${r.added.length} 个`)
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}

// ---------- 按角色调整（项目角色层单独覆盖） ----------
function fieldValue(roleId: string): string[] | undefined {
  const f = roleDetails.value[roleId]?.fields[props.kind]
  return f === undefined ? undefined : (f.value as string[])
}
function fieldSource(roleId: string): string {
  return roleDetails.value[roleId]?.fields[props.kind]?.source ?? ''
}
async function saveField(roleId: string, ids: string[]) {
  const dup = duplicateNameError(ids, id => entries.value.find(e => e.id === id)?.name ?? id)
  if (dup) { ElMessage.error(`${kindLabel.value}里选了两个同名的：${dup}`); matrixKey.value++; return }
  saving.value = true
  try {
    await configApi.setRoleFields(roleId, scopeProject.value, { [props.kind]: ids })
    roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value)
    ElMessage.success('已保存，下次派工生效')
  } catch (e) {
    ElMessage.error(msg(e))
    try { roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value) } catch { /* 忽略 */ }
    matrixKey.value++
  } finally { saving.value = false }
}
async function toggleRole(roleId: string, id: string) {
  const current = fieldValue(roleId) ?? []
  await saveField(roleId, toggleRegistryId(current, id))
}
async function stopRoleOverride(roleId: string) {
  saving.value = true
  try {
    await configApi.resetRoleFields(roleId, scopeProject.value, [props.kind])
    roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value)
    ElMessage.success('已恢复跟随项目')
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
function roleState(r: { id: string }): { text: string; own: boolean } {
  const v = fieldValue(r.id)
  if (v === undefined) return { text: '未管理', own: false }
  const src = fieldSource(r.id)
  const own = src === (inProject.value ? '项目' : '本机全局')
  return { text: own ? `单独配置：${v.length} 个` : (inProject.value ? '跟随项目' : `继承（${src}）`), own }
}

// ---------- 新增 / 修改 ----------
const editOpen = ref(false)
const editSaving = ref(false)
const mcpForm = reactive({ id: '', name: '', type: 'local', command: '', argsText: '', envText: '', url: '', headersText: '', description: '' })
const skillForm = reactive({ id: '', name: '', path: '' })
let editing = false

function openCreate() {
  editing = false
  Object.assign(mcpForm, { id: '', name: '', type: 'local', command: '', argsText: '', envText: '', url: '', headersText: '', description: '' })
  Object.assign(skillForm, { id: '', name: '', path: '' })
  editOpen.value = true
}
function openEdit(row: RegistryRow) {
  editing = true
  if (isMcp.value) {
    const m = row.entry ?? {}
    Object.assign(mcpForm, {
      id: row.id, name: row.name, type: (m.type as string) || (m.url ? 'remote' : 'local'),
      command: (m.command as string) ?? '', argsText: ((m.args as string[]) ?? []).join('\n'),
      envText: Object.entries((m.env as Record<string, string>) ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
      url: (m.url as string) ?? '', headersText: Object.entries((m.headers as Record<string, string>) ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
      description: (m.description as string) ?? '',
    })
  } else {
    Object.assign(skillForm, { id: row.id, name: row.name, path: row.path ?? '' })
  }
  editOpen.value = true
}
function lines(text: string): string[] {
  return text.split('\n').map(l => l.trim()).filter(Boolean)
}
function pairs(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of lines(text)) {
    const [k, ...rest] = line.split('=')
    if (k && rest.length) out[k.trim()] = rest.join('=')
  }
  return out
}
async function saveEntry() {
  editSaving.value = true
  try {
    const project = inProject.value ? projectId.value : undefined
    if (isMcp.value) {
      const entry: Record<string, unknown> = { name: mcpForm.name.trim() || mcpForm.id.trim(), type: mcpForm.type }
      if (mcpForm.type === 'local') {
        entry.command = mcpForm.command
        if (lines(mcpForm.argsText).length) entry.args = lines(mcpForm.argsText)
      } else {
        entry.url = mcpForm.url
      }
      if (Object.keys(pairs(mcpForm.envText)).length) entry.env = pairs(mcpForm.envText)
      if (Object.keys(pairs(mcpForm.headersText)).length) entry.headers = pairs(mcpForm.headersText)
      if (mcpForm.description.trim()) entry.description = mcpForm.description
      await mcpApi.save(mcpForm.id.trim(), entry, project)
    } else {
      await skillApi.save(skillForm.id.trim(), skillForm.path.trim(), skillForm.name.trim() || undefined, project)
    }
    ElMessage.success(editing ? '已保存' : '已登记')
    editOpen.value = false
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { editSaving.value = false }
}
async function removeEntry(row: RegistryRow) {
  const { ElMessageBox } = await import('element-plus')
  try {
    await ElMessageBox.confirm(`从登记表删除 ${row.id}？`, '二次确认', { type: 'warning' })
  } catch { return }
  try {
    await (isMcp.value ? mcpApi.remove(row.id) : skillApi.remove(row.id))
    ElMessage.success('已删除')
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } // 被角色 / 项目默认引用等服务端拒绝原因原样展示
}

// ---------- 从现有配置导入（次要入口） ----------
const importOpen = ref(false)
const importLoading = ref(false)
const importError = ref('')
const importSaving = ref(false)
const importQuery = ref('')
const importChecked = ref<Record<string, boolean>>({})

async function openImport() {
  importOpen.value = true
  importQuery.value = ''
  importChecked.value = {}
  importError.value = ''
  importLoading.value = true
  try {
    const r = isMcp.value ? await mcpApi.candidates(candScope.value) : await skillApi.candidates(candScope.value)
    candidates.value = r.candidates
  } catch (e) { importError.value = msg(e) } finally { importLoading.value = false }
}
const filteredCandidates = computed(() => filterCandidateGroups(candidates.value, importQuery.value))
const allSelectable = computed(() => selectableCandidateIds(filteredCandidates.value))
const allChecked = computed(() =>
  allSelectable.value.length > 0 && allSelectable.value.every(id => importChecked.value[id]))
function toggleAll(v: boolean) {
  for (const id of allSelectable.value) {
    if (v) importChecked.value[id] = true
    else delete importChecked.value[id]
  }
}
const checkedIds = computed(() => Object.entries(importChecked.value).filter(([, v]) => v).map(([id]) => id))
async function doImport() {
  if (!checkedIds.value.length) return
  importSaving.value = true
  try {
    const r = isMcp.value
      ? await mcpApi.importIds(checkedIds.value, inProject.value ? projectId.value : undefined)
      : await skillApi.importIds(checkedIds.value, inProject.value ? projectId.value : undefined)
    ElMessage.success(r.added.length ? `新登记 ${r.added.length} 个：${r.added.join('、')}` : '选中的都已登记过')
    importOpen.value = false
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { importSaving.value = false }
}
</script>

<template>
  <div class="reg" v-loading="loading">
    <DefineProject v-if="projectId && !projectDefined" :project-id="projectId" @defined="reload" />
    <template v-else>
      <!-- 1. 一句话现状 + 管理开关（仅项目视图） -->
      <div v-if="inProject" class="status-line">
        <span>{{ statusLine }}</span>
        <el-tooltip content="打开 = 本项目派工时只带勾选的下发；关闭 = 恢复未管理" placement="top">
          <el-switch :model-value="managed" :loading="saving" @change="(v: string | number | boolean) => toggleManaged(!!v)" />
        </el-tooltip>
        <span class="sub">由 agent-dispatch 管理本项目的{{ kindLabel }}</span>
      </div>
      <el-alert v-else type="info" :closable="false" show-icon class="note"
        :title="`全部项目视图：只看全局登记${kind === 'mcp' ? '的 MCP' : '的技能'}与全局角色层。项目里的${kindLabel}请先在左上角选择项目`" />

      <!-- 2. 主表：已登记的（本项目 + 全局）与发现未纳入的 -->
      <div class="tbl-head">
        <h2>{{ inProject ? '本项目的' : '全局' }}{{ kindLabel }}</h2>
        <div>
          <el-button size="small" @click="openImport">从现有配置导入</el-button>
          <el-button size="small" type="primary" @click="openCreate">登记{{ kindLabel }}</el-button>
        </div>
      </div>
      <el-table :data="rows" size="small" class="tbl">
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <div class="mono">{{ row.name }}</div>
            <div class="sub mono">{{ row.id }}</div>
            <div v-if="row.diff" class="sub mono diff">{{ row.diff }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="isMcp ? '命令 / 地址（遮罩）' : '目录'" min-width="220">
          <template #default="{ row }"><span class="mono">{{ rowSummary(row) }}</span></template>
        </el-table-column>
        <el-table-column label="归属" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            {{ ownerLabel(row) }}<span v-if="row.sources.length" class="sub"> · {{ row.sources[0] }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTone(row.status)" size="small">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #header>
            <el-button v-if="discoveredIds.length" size="small" link type="primary" :loading="saving" @click="includeAll">
              全部纳入（{{ discoveredIds.length }}）
            </el-button>
          </template>
          <template #default="{ row }">
            <template v-if="row.kind === 'candidate'">
              <el-button size="small" link type="primary" :loading="saving" @click="includeVariant(row)">纳入</el-button>
            </template>
            <template v-else>
              <el-button v-if="inProject" size="small" link type="primary" :disabled="!managed" :loading="saving"
                @click="toggleEnabled(row)">{{ enabled.includes(row.id) ? '停用' : '启用' }}</el-button>
              <el-button size="small" link type="primary" @click="openEdit(row)">修改</el-button>
              <el-button size="small" link type="danger" @click="removeEntry(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!rows.length" class="sub empty">
        还没有内容：本项目工程和全局来源里都没有发现{{ kindLabel }}，可以先登记一个
      </div>

      <!-- 3. 按角色调整（可选，默认折叠） -->
      <el-collapse class="roles-collapse">
        <el-collapse-item name="roles">
          <template #title>
            <b>按角色调整（可选）</b>
            <span class="sub" style="margin-left: 8px">
              {{ inProject ? '角色默认跟随项目，需要不同的在这里单独配置' : '全局角色层的启用情况' }}
            </span>
          </template>
          <div class="sub scope-note">
            列是{{ inProject ? `项目 ${projectId}` : '全局角色（当前视图按全局层读写）' }}；
            勾选即保存到该角色的 {{ kind }} 字段
          </div>
          <table class="matrix" v-if="scopeRoles.length" :key="matrixKey">
            <thead>
              <tr>
                <th class="corner">{{ kindLabel }}</th>
                <th v-for="r in scopeRoles" :key="r.id">
                  <div class="role-head">
                    <span>{{ r.title }}</span>
                    <span class="sub mono">{{ r.id }}</span>
                    <el-tag size="small" :type="roleState(r).own ? 'warning' : 'info'">{{ roleState(r).text }}</el-tag>
                    <el-button v-if="roleState(r).own" size="small" link @click.stop="stopRoleOverride(r.id)">恢复跟随</el-button>
                  </div>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="e in entries" :key="e.id">
                <td>
                  <div class="mono">{{ e.id }}</div>
                  <div class="sub">下发名 {{ e.name }}</div>
                </td>
                <td v-for="r in scopeRoles" :key="r.id" class="cell">
                  <el-checkbox v-if="fieldValue(r.id) !== undefined"
                    :model-value="(fieldValue(r.id) ?? []).includes(e.id)"
                    @update:model-value="toggleRole(r.id, e.id)" />
                </td>
              </tr>
              <tr v-if="!entries.length"><td :colspan="scopeRoles.length + 1" class="sub empty">登记表还是空的，先纳入或登记</td></tr>
            </tbody>
          </table>
          <div v-else class="sub">这个范围下还没有角色。</div>
        </el-collapse-item>
      </el-collapse>
    </template>

    <div class="footnote sub">{{ DISPATCH_PENDING_NOTE }}</div>

    <el-dialog v-model="editOpen" :title="editing ? `修改${kindLabel}登记` : `登记${kindLabel}`" width="560px">
      <el-form label-width="110px">
        <el-form-item label="登记 ID" required>
          <el-input v-if="isMcp" v-model="mcpForm.id" :disabled="editing" class="mono" placeholder="如 playwright@a04-test" />
          <el-input v-else v-model="skillForm.id" :disabled="editing" class="mono" />
        </el-form-item>
        <template v-if="isMcp">
          <el-form-item label="下发名称">
            <el-input v-model="mcpForm.name" placeholder="缺省等于登记 ID" />
          </el-form-item>
          <el-form-item label="类型">
            <el-radio-group v-model="mcpForm.type">
              <el-radio value="local">local（命令）</el-radio>
              <el-radio value="remote">remote（地址）</el-radio>
            </el-radio-group>
          </el-form-item>
          <template v-if="mcpForm.type === 'local'">
            <el-form-item label="命令" required><el-input v-model="mcpForm.command" class="mono" /></el-form-item>
            <el-form-item label="参数"><el-input v-model="mcpForm.argsText" type="textarea" :rows="3" class="mono" placeholder="一行一个" /></el-form-item>
            <el-form-item label="环境变量">
              <el-input v-model="mcpForm.envText" type="textarea" :rows="2" class="mono" placeholder="KEY=value，一行一个" />
            </el-form-item>
          </template>
          <template v-else>
            <el-form-item label="地址" required><el-input v-model="mcpForm.url" class="mono" /></el-form-item>
            <el-form-item label="请求头">
              <el-input v-model="mcpForm.headersText" type="textarea" :rows="2" class="mono" placeholder="KEY=value，一行一个" />
            </el-form-item>
          </template>
          <el-form-item label="说明"><el-input v-model="mcpForm.description" /></el-form-item>
          <el-alert type="info" :closable="false" show-icon
            title="命令 / 参数 / 环境变量里已经是遮罩值：留着 •••• 不改就是保留原值" />
        </template>
        <template v-else>
          <el-form-item label="下发名称"><el-input v-model="skillForm.name" placeholder="缺省取目录名" /></el-form-item>
          <el-form-item label="技能目录" required>
            <el-input v-model="skillForm.path" class="mono" placeholder="目录里要有 SKILL.md" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="editOpen = false">取消</el-button>
        <el-button type="primary" :loading="editSaving"
          :disabled="!(isMcp ? (mcpForm.id.trim() && (mcpForm.type === 'local' ? mcpForm.command.trim() : mcpForm.url.trim())) : (skillForm.id.trim() && skillForm.path.trim()))"
          @click="saveEntry">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importOpen" :title="`从现有配置导入${kindLabel}`" width="720px">
      <div class="sub" style="margin-bottom: 8px">只读取、不修改这些配置文件；同名有多个变体的按登记 ID 和来源区分</div>
      <el-alert v-if="importError" type="error" :closable="false" show-icon :title="importError" style="margin-bottom: 8px" />
      <div class="toolbar">
        <el-input v-model="importQuery" placeholder="搜索名称、登记 ID、来源、说明" clearable size="small" style="width: 260px" />
        <el-checkbox :model-value="allChecked" size="small" @update:model-value="toggleAll">全选（未登记的）</el-checkbox>
        <span class="sub">已勾选 {{ checkedIds.length }} 个</span>
      </div>
      <div v-loading="importLoading" class="cand-list">
        <div v-for="g in filteredCandidates" :key="g.name" class="cand-group">
          <div class="cand-name">
            <b>{{ g.name }}</b>
            <el-tag v-if="g.conflict" type="warning" size="small">同名有 {{ g.variants.length }} 个变体</el-tag>
          </div>
          <div v-for="v in g.variants" :key="v.id" class="cand-var" :class="{ regd: v.registered }">
            <el-checkbox v-model="importChecked[v.id]" :disabled="v.registered" />
            <span class="mono">{{ v.id }}</span>
            <span class="sub">{{ v.source }}</span>
            <el-tag v-if="v.registered" type="info" size="small">已登记</el-tag>
            <span v-if="!isMcp" class="sub mono">{{ v.path }}</span>
            <span v-if="v.description" class="sub desc">{{ v.description }}</span>
            <template v-if="g.conflict">
              <span class="sub mono diff">
                {{ isMcp ? mcpEntrySummary(v.entry) : '' }}
                {{ Object.entries(v.diff ?? {}).map(([k, x]) => `${k}=${JSON.stringify(x)}`).join(' ') }}
              </span>
            </template>
          </div>
        </div>
        <el-empty v-if="!importLoading && !filteredCandidates.length"
          :description="importError || '没有找到候选'" :image-size="60" />
      </div>
      <template #footer>
        <el-button @click="importOpen = false">取消</el-button>
        <el-button type="primary" :loading="importSaving" :disabled="!checkedIds.length" @click="doImport">
          导入 {{ checkedIds.length }} 个
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.reg { padding: 2px; overflow: auto; }
.note { margin-bottom: 10px; }
.status-line { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 12px; border-radius: 8px;
  background: var(--el-fill-color-light); margin-bottom: 10px; }
.tbl-head { display: flex; align-items: center; justify-content: space-between; margin: 6px 0 8px; }
.tbl-head h2 { margin: 0; font-size: 15px; }
.tbl { width: 100%; }
.roles-collapse { margin-top: 14px; border-top: 1px solid var(--el-border-color); }
.scope-note { margin-bottom: 8px; }
.matrix { border-collapse: collapse; width: 100%; }
.matrix th, .matrix td { border: 1px solid var(--el-border-color); padding: 6px 8px; vertical-align: top; text-align: left; }
.matrix th.corner { width: 220px; }
.role-head { display: flex; flex-direction: column; gap: 2px; min-width: 130px; align-items: flex-start; }
.cell { text-align: center; }
.empty { padding: 12px; text-align: center; }
.cand-list { max-height: 50vh; overflow: auto; border: 1px solid var(--el-border-color); border-radius: 6px; padding: 8px; }
.cand-group { margin-bottom: 10px; }
.cand-name { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.cand-var { display: flex; align-items: center; gap: 8px; padding: 3px 0 3px 18px; flex-wrap: wrap; }
.cand-var.regd { opacity: .55; }
.cand-var .desc { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cand-var .diff, .diff { max-width: 100%; }
.footnote { margin-top: 16px; padding-top: 8px; border-top: 1px solid var(--el-border-color); }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
