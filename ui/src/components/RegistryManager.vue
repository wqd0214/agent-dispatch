<script setup lang="ts">
// MCP / 技能登记页共用：登记表（增删改，遮罩占位保留原值）、从现有配置导入（按名称分组、可搜索全选）、
// 「哪些角色在用」对照表（行是登记条目、列是当前项目可用的角色，勾选即保存到该角色对应字段）。
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { configApi, mcpApi, skillApi } from '../api'
import type { CandidateGroup, ConfigOverview, McpEntry, RoleDetail, SkillEntry } from '../types'
import { useCurrentProject } from '../composables/useProject'
import {
  DISPATCH_PENDING_NOTE, duplicateNameError, filterCandidateGroups, mcpEntrySummary,
  selectableCandidateIds, toggleRegistryId, usedByLabel,
} from '../lib/registry'
import { projectRoleRows, type RoleListRow } from '../lib/roles'

const props = defineProps<{ kind: 'mcp' | 'skills' }>()
const isMcp = computed(() => props.kind === 'mcp')
const { selected: projectId } = useCurrentProject()

const config = ref<ConfigOverview | null>(null)
const entries = ref<(McpEntry | SkillEntry)[]>([])
const roleDetails = ref<Record<string, RoleDetail>>({})
const loading = ref(false)
const savingRole = ref('')
const matrixKey = ref(0) // 保存被拒后强制重画对照表，勾选框回到真实值

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }

async function reload() {
  loading.value = true
  try {
    const [cfg, page] = await Promise.all([
      configApi.overview(),
      isMcp.value ? mcpApi.page() : skillApi.page(),
    ])
    config.value = cfg
    entries.value = isMcp.value ? (page as { mcp: McpEntry[] }).mcp : (page as { skills: SkillEntry[] }).skills
    await reloadRoleDetails()
  } catch (e) { ElMessage.error(msg(e)) } finally { loading.value = false }
}

const scopeRows = computed(() => projectRoleRows(config.value, projectId.value))
const scopeRoles = computed<RoleListRow[]>(() => scopeRows.value.rows)
const projectDefined = computed(() => scopeRows.value.defined)
// 对照表和角色页一致：项目没定义或全部项目视图都按全局层读写
const scopeProject = computed(() => projectDefined.value ? projectId.value : '')

async function reloadRoleDetails() {
  const out: Record<string, RoleDetail> = {}
  for (const r of scopeRoles.value) {
    try { out[r.id] = await configApi.roleDetail(r.id, scopeProject.value) } catch { /* 角色细节读不到就当未管理 */ }
  }
  roleDetails.value = out
}
watch(scopeProject, reloadRoleDetails)
void reload()

function roleTitle(project: string, role: string): string {
  if (!config.value) return role
  if (!project) return config.value.roles.find(r => r.id === role)?.title ?? role
  return config.value.projects.find(p => p.id === project)?.roles.find(r => r.id === role)?.title ?? role
}
const kindLabel = computed(() => (isMcp.value ? 'MCP 服务' : '技能'))

/** 角色的 mcp / skills 字段：undefined = 未管理；列表 = 已管理 */
function fieldValue(roleId: string): string[] | undefined {
  const f = roleDetails.value[roleId]?.fields[props.kind]
  return f === undefined ? undefined : (f.value as string[])
}
function fieldSource(roleId: string): string {
  return roleDetails.value[roleId]?.fields[props.kind]?.source ?? ''
}

async function saveField(roleId: string, ids: string[]) {
  const dup = duplicateNameError(ids, id => entries.value.find(e => e.id === id)?.name ?? id)
  if (dup) {
    ElMessage.error(`${kindLabel.value}里选了两个同名的：${dup}`)
    matrixKey.value++
    return
  }
  savingRole.value = roleId
  try {
    await configApi.setRoleFields(roleId, scopeProject.value, { [props.kind]: ids })
    roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value)
    ElMessage.success('已保存，下次派工生效')
  } catch (e) {
    ElMessage.error(msg(e))
    try { roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value) } catch { /* 忽略 */ }
    matrixKey.value++
  } finally { savingRole.value = '' }
}
async function toggle(roleId: string, id: string) {
  const current = fieldValue(roleId) ?? []
  await saveField(roleId, toggleRegistryId(current, id))
}
async function startManage(roleId: string) {
  await saveField(roleId, []) // 空列表 = 由 agent-dispatch 管理，一个都不带
}
async function stopManage(roleId: string) {
  savingRole.value = roleId
  try {
    await configApi.resetRoleFields(roleId, scopeProject.value, [props.kind])
    roleDetails.value[roleId] = await configApi.roleDetail(roleId, scopeProject.value)
    ElMessage.success('已恢复不管理：执行者照旧用它自己的配置')
  } catch (e) { ElMessage.error(msg(e)) } finally { savingRole.value = '' }
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
function openEdit(row: McpEntry | SkillEntry) {
  editing = true
  if (isMcp.value) {
    const m = row as McpEntry
    Object.assign(mcpForm, {
      id: m.id, name: m.name, type: m.type || (m.url ? 'remote' : 'local'),
      command: m.command ?? '', argsText: (m.args ?? []).join('\n'),
      envText: Object.entries(m.env ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
      url: m.url ?? '', headersText: Object.entries(m.headers ?? {}).map(([k, v]) => `${k}=${v}`).join('\n'),
      description: m.description ?? '',
    })
  } else {
    const s = row as SkillEntry
    Object.assign(skillForm, { id: s.id, name: s.name, path: s.path })
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
      await mcpApi.save(mcpForm.id.trim(), entry)
    } else {
      await skillApi.save(skillForm.id.trim(), skillForm.path.trim(), skillForm.name.trim() || undefined)
    }
    ElMessage.success(editing ? '已保存' : '已登记')
    editOpen.value = false
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { editSaving.value = false }
}
async function removeEntry(row: McpEntry | SkillEntry) {
  const { ElMessageBox } = await import('element-plus')
  try {
    await ElMessageBox.confirm(`从登记表删除 ${row.id}？`, '二次确认', { type: 'warning' })
  } catch { return }
  try {
    await (isMcp.value ? mcpApi.remove(row.id) : skillApi.remove(row.id))
    ElMessage.success('已删除')
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } // 被角色引用等服务端拒绝原因原样展示
}

// ---------- 从现有配置导入 ----------
const importOpen = ref(false)
const importLoading = ref(false)
const importSaving = ref(false)
const candidates = ref<CandidateGroup[]>([])
const importQuery = ref('')
const importChecked = ref<Record<string, boolean>>({})

async function openImport() {
  importOpen.value = true
  importQuery.value = ''
  importChecked.value = {}
  importLoading.value = true
  try {
    const r = isMcp.value ? await mcpApi.candidates() : await skillApi.candidates()
    candidates.value = r.candidates
  } catch (e) { ElMessage.error(msg(e)) } finally { importLoading.value = false }
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
    const r = isMcp.value ? await mcpApi.importIds(checkedIds.value) : await skillApi.importIds(checkedIds.value)
    ElMessage.success(r.added.length ? `新登记 ${r.added.length} 个：${r.added.join('、')}` : '选中的都已登记过')
    importOpen.value = false
    await reload()
  } catch (e) { ElMessage.error(msg(e)) } finally { importSaving.value = false }
}
</script>

<template>
  <div class="reg" v-loading="loading">
    <el-alert type="info" :closable="false" show-icon class="note" :title="DISPATCH_PENDING_NOTE" />
    <el-alert v-if="projectId && !projectDefined" type="warning" :closable="false" show-icon class="note"
      title="当前项目还没有在 agent-dispatch 里定义，下面的角色按全局层读写" />

    <h2>{{ kindLabel }}登记表</h2>
    <div class="toolbar">
      <el-button size="small" type="primary" @click="openCreate">登记{{ kindLabel }}</el-button>
      <el-button size="small" @click="openImport">从现有配置导入</el-button>
      <span class="sub">只读取、不修改来源配置文件</span>
    </div>
    <el-table :data="entries" size="small" class="tbl">
      <el-table-column prop="id" label="登记 ID" min-width="150" />
      <el-table-column prop="name" label="下发名称" min-width="110" />
      <el-table-column v-if="isMcp" label="类型与命令 / 地址" min-width="240">
        <template #default="{ row }">
          <span class="mono">{{ mcpEntrySummary(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column v-else label="目录" min-width="240">
        <template #default="{ row }"><span class="mono">{{ row.path }}</span></template>
      </el-table-column>
      <el-table-column prop="description" label="说明" min-width="140" show-overflow-tooltip />
      <el-table-column label="来源" min-width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ (row.sources ?? []).join('、') || '手动登记' }}</template>
      </el-table-column>
      <el-table-column label="被哪些角色使用" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.used_by.length ? usedByLabel(row.used_by, roleTitle) : '-' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="130">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openEdit(row)">修改</el-button>
          <el-button size="small" link type="danger" @click="removeEntry(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <h2>哪些角色在用</h2>
    <div class="sub scope-note">
      列是{{ projectId && projectDefined ? `项目 ${projectId}` : '全局角色（当前视图按全局层读写）' }}；
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
              <el-switch v-if="fieldValue(r.id) === undefined" size="small"
                :loading="savingRole === r.id" @change="startManage(r.id)" />
              <el-tooltip v-else content="关闭后不再管理：执行者照旧用它自己的配置" placement="top">
                <el-switch :model-value="true" size="small" :loading="savingRole === r.id" @change="stopManage(r.id)" />
              </el-tooltip>
            </div>
            <div v-if="fieldValue(r.id) === undefined" class="sub unmanaged">未管理：执行者照旧用它自己的配置。打开开关开始管理</div>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in entries" :key="e.id">
          <td>
            <div class="mono">{{ e.id }}</div>
            <div class="sub">下发名 {{ e.name }}</div>
          </td>
          <td v-for="r in scopeRoles" :key="r.id" class="cell" :class="{ busy: savingRole === r.id }">
            <el-checkbox v-if="fieldValue(r.id) !== undefined"
              :model-value="(fieldValue(r.id) ?? []).includes(e.id)"
              @update:model-value="toggle(r.id, e.id)" />
          </td>
        </tr>
        <tr v-if="!entries.length"><td :colspan="scopeRoles.length + 1" class="sub empty">登记表还是空的，先导入或登记</td></tr>
      </tbody>
    </table>
    <div v-else class="sub">这个范围下还没有角色。</div>

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
        <el-empty v-if="!importLoading && !filteredCandidates.length" description="没有找到候选" :image-size="60" />
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
h2 { margin: 14px 0 8px; font-size: 15px; }
.toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.tbl { width: 100%; }
.scope-note { margin-bottom: 8px; }
.matrix { border-collapse: collapse; width: 100%; }
.matrix th, .matrix td { border: 1px solid var(--el-border-color); padding: 6px 8px; vertical-align: top; text-align: left; }
.matrix th.corner { width: 220px; }
.role-head { display: flex; flex-direction: column; gap: 2px; min-width: 130px; }
.unmanaged { max-width: 150px; white-space: normal; }
.cell { text-align: center; }
.cell.busy { opacity: .5; }
.empty { padding: 12px; text-align: center; }
.cand-list { max-height: 50vh; overflow: auto; border: 1px solid var(--el-border-color); border-radius: 6px; padding: 8px; }
.cand-group { margin-bottom: 10px; }
.cand-name { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.cand-var { display: flex; align-items: center; gap: 8px; padding: 3px 0 3px 18px; flex-wrap: wrap; }
.cand-var.regd { opacity: .55; }
.cand-var .desc { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cand-var .diff { max-width: 100%; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
</style>
