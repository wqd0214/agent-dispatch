<script setup lang="ts">
// 角色页：左边当前项目（全部项目时为全局）的角色列表，右边选中角色的配置。
// 字段带来源标签，只提交改过的字段；保存走 /api/config/roles 系列接口。
import { computed, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { configApi, modelApi, mcpApi, skillApi } from '../api'
import type { ConfigOverview, ModelPage, PoolEntry, RoleDetail } from '../types'
import { fieldOverridden, hasOwnLayer, inheritLabel, poolRows, projectRoleRows, summarizeRole } from '../lib/roles'
import { useCurrentProject } from '../composables/useProject'
import { useRouter } from 'vue-router'
import DefineProject from '../components/DefineProject.vue'

const { selected: projectId } = useCurrentProject()
const route = useRoute()
const router = useRouter()

const config = ref<ConfigOverview | null>(null)
const modelsPage = ref<ModelPage | null>(null)
const detail = ref<RoleDetail | null>(null)
const roleId = ref('')
const loading = ref(false)
const saving = ref(false)

async function reloadConfig() {
  try { config.value = await configApi.overview() } catch (e) { ElMessage.error(msg(e)) }
}
async function reloadModels() {
  try { modelsPage.value = await modelApi.page() } catch (e) { ElMessage.error(msg(e)) }
}
async function reloadDetail() {
  if (!roleId.value) { detail.value = null; return }
  loading.value = true
  try {
    detail.value = await configApi.roleDetail(roleId.value, detailProject.value)
    syncForm()
    fallbackDirty.value = false
  } catch (e) {
    detail.value = null
    ElMessage.error(msg(e))
  } finally { loading.value = false }
}
function msg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
function saved() {
  ElMessage.success('已保存，下次派工生效')
}

const roleRows = computed(() => projectRoleRows(config.value, projectId.value))
const roles = computed(() => roleRows.value.rows)
const projectDefined = computed(() => roleRows.value.defined)
const project = computed(() => projectDefined.value
  ? config.value?.projects.find(p => p.id === projectId.value) ?? null : null)
// 项目没定义过时，详情按全局层读（读得到继承的全局配置），写入也一样写全局层
const detailProject = computed(() => projectDefined.value ? projectId.value : '')
const repoOptions = computed(() => (project.value?.repos ?? []).map(r => ({ value: r.id, label: r.name })))
function repoName(id: string): string {
  return project.value?.repos.find(r => r.id === id)?.name ?? id
}

watch(roleId, reloadDetail)
watch(projectId, () => { roleId.value = roles.value[0]?.id ?? '' })
watch(detailProject, () => { roleId.value = roles.value[0]?.id ?? '' })
watch(() => route.query.role, q => {
  if (typeof q === 'string' && roles.value.some(r => r.id === q)) roleId.value = q
})
void reloadConfig().then(() => {
  const q = typeof route.query.role === 'string' ? route.query.role : ''
  roleId.value = roles.value.some(r => r.id === q) ? q : (roles.value[0]?.id ?? '')
})
void reloadModels()

// ---------- 基本 ----------
interface BasicForm { title: string; description: string; repos: string[]; mode: string; timeout_min: number | null }
const basic = reactive<BasicForm>({ title: '', description: '', repos: [], mode: 'headless', timeout_min: null })
let basicOrigin = ''

function fieldValue<T>(key: string, fallback: T): T {
  const f = detail.value?.fields[key]
  return f === undefined ? fallback : (f.value as T)
}
function syncForm() {
  const d = detail.value
  if (!d) return
  basic.title = d.title
  basic.description = fieldValue('description', '')
  basic.repos = [...d.repos]
  basic.mode = fieldValue('mode', 'headless')
  basic.timeout_min = fieldValue<number | null>('timeout_min', null)
  basicOrigin = JSON.stringify([basic.title, basic.description, basic.repos, basic.mode, basic.timeout_min])
  fallbackModel.value = fieldValue('model', '')
}
const basicDirty = computed(() => basicOrigin && JSON.stringify(
  [basic.title, basic.description, basic.repos, basic.mode, basic.timeout_min]) !== basicOrigin)

async function saveBasic() {
  if (!detail.value) return
  const fields: Record<string, unknown> = {}
  const o = JSON.parse(basicOrigin) as [string, string, string[], string, number | null]
  if (basic.title !== o[0]) fields.title = basic.title
  if (basic.description !== o[1]) fields.description = basic.description
  if (JSON.stringify(basic.repos) !== JSON.stringify(o[2])) fields.repos = basic.repos
  if (basic.mode !== o[3]) fields.mode = basic.mode
  if (basic.timeout_min !== o[4]) fields.timeout_min = basic.timeout_min
  if (!Object.keys(fields).length) return
  saving.value = true
  try {
    await configApi.setRoleFields(roleId.value, detailProject.value, fields)
    await reloadDetail(); await reloadConfig(); saved()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
async function resetField(key: string) {
  try {
    await configApi.resetRoleFields(roleId.value, detailProject.value, [key])
    await reloadDetail(); await reloadConfig(); saved()
  } catch (e) { ElMessage.error(msg(e)) }
}
function sourceOf(key: string): string {
  return detail.value?.fields[key]?.source ?? '未设置'
}

// 项目视图下选中继承自全局的角色：只读展示，点「为本项目单独配置」后才在项目层写入
const inheritedHere = computed(() => !!projectId.value && projectDefined.value && detail.value?.scope === '全局')
async function ownLayer() {
  if (!detail.value) return
  saving.value = true
  try {
    await configApi.addRole({ id: roleId.value, project: projectId.value, title: detail.value.title })
    await reloadDetail(); await reloadConfig()
    ElMessage.success('已在项目层创建这一角色的单独配置，现在可以改了')
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
async function restoreInherit() {
  await deleteRole()
  await reloadDetail()
}

// ---------- MCP / 技能（只展示选中条目和来源层，勾选去对应页做）----------
const regNames = { mcp: ref<Record<string, string>>({}), skills: ref<Record<string, string>>({}) }
function loadRegNames() {
  const p = projectDefined.value ? projectId.value : undefined
  void mcpApi.page(p).then(r => { regNames.mcp.value = Object.fromEntries(r.mcp.map(e => [e.id, e.name])) }).catch(() => {})
  void skillApi.page(p).then(r => { regNames.skills.value = Object.fromEntries(r.skills.map(e => [e.id, e.name])) }).catch(() => {})
}
watch(projectId, loadRegNames, { immediate: true })
function regField(key: 'mcp' | 'skills'): string[] | null {
  const f = detail.value?.fields[key]
  return f === undefined ? null : (f.value as string[])
}
// 值来自哪一层：项目视图下区分「跟随项目」与「单独配置」
function regSourceLabel(key: 'mcp' | 'skills'): string {
  const src = sourceOf(key)
  if (src === '项目默认') return '跟随项目（项目默认）'
  if (src === '项目') return '单独配置（本项目角色）'
  if (src === '本机全局') return '单独配置（全局角色）'
  return src
}

// ---------- 模型池 ----------
const poolChecked = ref<Record<string, number>>({})
const backendsOf = computed(() => {
  const byBackend = new Map<string, ModelPage['models']>()
  for (const m of modelsPage.value?.models ?? []) {
    if (!byBackend.has(m.backend)) byBackend.set(m.backend, [])
    byBackend.get(m.backend)!.push(m)
  }
  return [...byBackend.entries()].map(([backend, models]) => ({ backend, models }))
})
const poolSummary = computed(() => poolRows(
  Object.entries(poolChecked.value).filter(([, w]) => w > 0).map(([model, weight]) => ({ model, weight }))))
const hasPool = computed(() => !!detail.value?.fields.model_pool)
const fallbackModel = ref('')
const fallbackDirty = ref(false)

function syncPool() {
  const pool = fieldValue<PoolEntry[] | null>('model_pool', null) || []
  poolChecked.value = Object.fromEntries(pool.map(e => [e.model, Number(e.weight) || 1]))
}
watch(detail, syncPool)

async function savePool() {
  const entries = Object.entries(poolChecked.value).filter(([, w]) => w > 0)
    .map(([model, weight]) => ({ model, weight }))
  saving.value = true
  try {
    await modelApi.savePool(roleId.value, detailProject.value, entries.length ? entries : null)
    await reloadDetail(); await reloadModels(); saved()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
async function saveFallback() {
  await doSetFields({ backend: fieldValue('backend', ''), model: fallbackModel.value })
}
async function doSetFields(fields: Record<string, unknown>) {
  saving.value = true
  try {
    await configApi.setRoleFields(roleId.value, detailProject.value, fields)
    fallbackDirty.value = false
    await reloadDetail(); saved()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}

// ---------- 岗位说明书 ----------
const briefText = ref('')
const briefDirty = ref(false)
function syncBrief() { briefText.value = detail.value?.brief.text ?? ''; briefDirty.value = false }
watch(detail, syncBrief)
async function saveBrief() {
  saving.value = true
  try {
    await configApi.setBrief(roleId.value, detailProject.value, briefText.value)
    await reloadDetail(); saved()
  } catch (e) { ElMessage.error(msg(e)) } finally { saving.value = false }
}
async function resetBrief() {
  try {
    await configApi.resetBrief(roleId.value, detailProject.value)
    await reloadDetail(); saved()
  } catch (e) { ElMessage.error(msg(e)) }
}

// ---------- 新建 / 删除 ----------
const createOpen = ref(false)
const createForm = reactive({ id: '', title: '', description: '', repos: [] as string[], backend: '', mode: 'headless' })
function openCreate() {
  Object.assign(createForm, { id: '', title: '', description: '', repos: [], backend: createForm.backend || 'pi', mode: 'headless' })
  createOpen.value = true
}
async function createRole() {
  try {
    await configApi.addRole({
      id: createForm.id, project: projectId.value || undefined,
      title: createForm.title || undefined, description: createForm.description || undefined,
      repos: createForm.repos.length ? createForm.repos : undefined,
      backend: createForm.backend || undefined, mode: createForm.mode,
    })
    createOpen.value = false
    await reloadConfig()
    roleId.value = createForm.id
    ElMessage.success('已创建')
  } catch (e) { ElMessage.error(msg(e)) }
}
async function deleteRole() {
  if (!detail.value) return
  try {
    await ElMessageBoxConfirm()
  } catch { return }
  try {
    await configApi.deleteRole(roleId.value, detailProject.value)
    ElMessage.success('已删除这一层')
    await reloadConfig()
    if (!roles.value.some(r => r.id === roleId.value)) roleId.value = roles.value[0]?.id ?? ''
    else await reloadDetail()
  } catch (e) { ElMessage.error(msg(e)) }
}
async function ElMessageBoxConfirm() {
  const { ElMessageBox } = await import('element-plus')
  await ElMessageBox.confirm(`删除「${detail.value?.title}」在${projectId.value ? '本项目' : '本机全局'}的定义？上层定义会重新生效。`, '二次确认', { type: 'warning' })
}
</script>

<template>
  <div class="page">
  <DefineProject v-if="projectId && !projectDefined" :project-id="projectId" @defined="reloadConfig" />
  <div class="roles">
    <div class="list">
      <div class="list-head">
        <span>{{ projectId ? (project?.name ?? projectId) : '全局角色' }}</span>
        <el-button v-if="projectDefined" size="small" type="primary" @click="openCreate">新建角色</el-button>
      </div>
      <div v-for="r in roles" :key="r.id" class="item" :class="{ sel: r.id === roleId }" @click="roleId = r.id">
        <div class="t">{{ r.title }}
          <el-tag v-if="inheritLabel(r.scope, !!projectId)" size="small" type="info">{{ inheritLabel(r.scope, !!projectId) }}</el-tag>
        </div>
        <div class="sub mono">{{ r.id }}</div>
      </div>
      <div v-if="!roles.length" class="sub empty">还没有角色</div>
    </div>
    <div class="panel" v-loading="loading">
      <template v-if="detail">
        <el-alert v-if="inheritedHere" type="info" :closable="false" show-icon class="hint" title="这个角色继承自全局，下面是全局的配置（只读）">
          <el-button size="small" type="primary" :loading="saving" @click="ownLayer">为本项目单独配置</el-button>
        </el-alert>
        <div class="sum">{{ summarizeRole(detail, repoName) }}</div>

        <h3>基本</h3>
        <el-form label-width="110px" class="form">
          <el-form-item label="称呼">
            <el-input v-model="basic.title" :disabled="inheritedHere" />
            <span class="src sub">来源：{{ sourceOf('title') }}</span><el-button v-if="fieldOverridden(detail, 'title')" link size="small" @click="resetField('title')">恢复继承</el-button>
          </el-form-item>
          <el-form-item label="说明">
            <el-input v-model="basic.description" type="textarea" :rows="2" :disabled="inheritedHere" />
            <span class="src sub">来源：{{ sourceOf('description') }}</span><el-button v-if="fieldOverridden(detail, 'description')" link size="small" @click="resetField('description')">恢复继承</el-button>
          </el-form-item>
          <el-form-item label="负责的工程">
            <el-select v-if="project" v-model="basic.repos" multiple placeholder="留空表示全部工程" :disabled="inheritedHere">
              <el-option v-for="r in repoOptions" :key="r.value" :value="r.value" :label="r.label" />
            </el-select>
            <span v-else class="sub">全局角色不绑定具体工程</span>
            <span class="src sub">来源：{{ sourceOf('repos') }}</span><el-button v-if="fieldOverridden(detail, 'repos')" link size="small" @click="resetField('repos')">恢复继承</el-button>
          </el-form-item>
          <el-form-item label="运行方式">
            <el-select v-model="basic.mode" style="width: 160px" :disabled="inheritedHere">
              <el-option value="headless" label="headless" /><el-option value="tmux" label="tmux" />
            </el-select>
            <span class="src sub">来源：{{ sourceOf('mode') }}</span><el-button v-if="fieldOverridden(detail, 'mode')" link size="small" @click="resetField('mode')">恢复继承</el-button>
          </el-form-item>
          <el-form-item label="单轮超时">
            <el-input-number v-model="basic.timeout_min" :min="1" :step="10" :disabled="inheritedHere" /> 分钟
            <span class="src sub">来源：{{ sourceOf('timeout_min') }}</span><el-button v-if="fieldOverridden(detail, 'timeout_min')" link size="small" @click="resetField('timeout_min')">恢复继承</el-button>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :disabled="inheritedHere || !basicDirty" :loading="saving" @click="saveBasic">保存基本设置</el-button>
          </el-form-item>
        </el-form>

        <h3>Agent 工具与模型</h3>
        <el-alert v-if="!hasPool" type="info" :closable="false" show-icon class="hint"
          :title="`未配置模型池，派工使用固定模型：${fieldValue('backend', '（未设置）')} / ${fallbackModel || '（由工具决定）'}`" />
        <div v-for="g in backendsOf" :key="g.backend" class="pool-group">
          <div class="pool-head">
            <b>{{ g.backend }}</b>
            <span class="sub" v-if="modelsPage?.tools.find(t => t.name === g.backend)?.enabled === false">（工具已停用）</span>
          </div>
          <div v-for="m in g.models" :key="m.alias" class="pool-row" :class="{ off: !m.enabled }">
            <el-checkbox :model-value="m.alias in poolChecked && poolChecked[m.alias] > 0"
              :disabled="!m.enabled || inheritedHere"
              @update:model-value="(on: boolean) => { if (on) poolChecked[m.alias] = 1; else delete poolChecked[m.alias] }">
              {{ m.alias }}
            </el-checkbox>
            <template v-if="m.alias in poolChecked && poolChecked[m.alias] > 0">
              <span class="sub">比例</span>
              <el-input-number v-model="poolChecked[m.alias]" :min="1" size="small" style="width: 100px" />
              <span class="sub">{{ poolSummary.find(r => r.model === m.alias)?.pct ?? 0 }}%</span>
            </template>
            <span class="sub off-note">{{ !m.enabled ? '已停用' : m.paused ? '暂停中' : '' }}</span>
          </div>
        </div>
        <div v-if="!modelsPage?.models.length" class="sub">还没有登记模型。到「全局 → 工具与模型」页导入或登记。</div>
        <el-button type="primary" :loading="saving" class="save" :disabled="inheritedHere" @click="savePool">
          {{ hasPool ? '保存模型池' : '保存为模型池' }}
        </el-button>

        <template v-if="!hasPool">
          <h4>固定模型（回退用）</h4>
          <el-form label-width="110px" class="form" inline>
            <el-form-item label="工具">
              <el-select :model-value="fieldValue('backend', '')" style="width: 140px"
                @update:model-value="(v: string) => doSetFields({ backend: v })">
                <el-option v-for="t in modelsPage?.tools ?? []" :key="t.name" :value="t.name" :label="t.name" />
              </el-select>
              <span class="src sub">来源：{{ sourceOf('backend') }}</span>
            </el-form-item>
            <el-form-item label="模型">
              <el-input v-model="fallbackModel" style="width: 260px" @input="fallbackDirty = true" placeholder="留空由工具决定" />
              <span class="src sub">来源：{{ sourceOf('model') }}</span>
            </el-form-item>
            <el-form-item>
              <el-button :disabled="!fallbackDirty" :loading="saving" @click="saveFallback">保存固定模型</el-button>
            </el-form-item>
          </el-form>
        </template>

        <h3>岗位说明书</h3>
        <div class="sub brief-src">来源：{{ detail.brief.source || '无' }}<template v-if="detail.brief.path"> · {{ detail.brief.path }}</template></div>
        <el-input v-model="briefText" type="textarea" :rows="10" class="mono" :disabled="inheritedHere" @input="briefDirty = true" />
        <div class="btns">
          <el-button type="primary" :disabled="inheritedHere || !briefDirty" :loading="saving" @click="saveBrief">保存岗位说明书</el-button>
          <el-button :disabled="inheritedHere || (detail.brief.source !== '项目' && detail.brief.source !== '本机全局')"
            @click="resetBrief">恢复继承</el-button>
        </div>

        <h3>MCP 与技能</h3>
        <div v-for="key in (['mcp', 'skills'] as const)" :key="key" class="regs">
          <b>{{ key === 'mcp' ? 'MCP 服务' : '技能' }}</b>
          <span v-if="regField(key) === null" class="sub">未管理：执行者照旧用它自己的配置</span>
          <span v-else-if="!regField(key)!.length" class="sub">由 agent-dispatch 管理，一个都不带（{{ regSourceLabel(key) }}）</span>
          <template v-else>
            <el-tag v-for="id in regField(key)!" :key="id" size="small" class="reg-tag"
              :title="`登记 ID ${id}，下发名 ${regNames[key].value[id] ?? id}`">
              {{ regNames[key].value[id] ?? id }}
            </el-tag>
            <span class="sub">{{ regSourceLabel(key) }}</span>
          </template>
          <el-button link size="small" type="primary"
            @click="router.push(key === 'mcp' ? '/settings/mcp' : '/settings/skills')">
            去{{ key === 'mcp' ? ' MCP ' : '技能' }}页调整
          </el-button>
        </div>

        <div class="danger">
          <el-button v-if="projectId && detail.scope === '项目覆盖全局'" :loading="saving" @click="restoreInherit">恢复继承全局</el-button>
          <el-button type="danger" plain :disabled="!hasOwnLayer(detail)"
            :title="hasOwnLayer(detail) ? '' : '随工具发布的角色不能删除，只能在项目或本机全局层覆盖'"
            @click="deleteRole">删除这一层</el-button>
          <span v-if="!hasOwnLayer(detail)" class="sub">随工具发布的角色不能删除，只能覆盖</span>
        </div>
      </template>
      <el-empty v-else-if="!loading" description="从左边选一个角色" />
    </div>
  </div>

    <el-dialog v-model="createOpen" title="新建角色" width="480px">
      <el-form label-width="110px">
        <el-form-item label="角色ID" required>
          <el-input v-model="createForm.id" placeholder="如 ops，只能字母数字和 -_." />
        </el-form-item>
        <el-form-item label="称呼"><el-input v-model="createForm.title" placeholder="如 运维工程师" /></el-form-item>
        <el-form-item label="说明"><el-input v-model="createForm.description" /></el-form-item>
        <el-form-item v-if="projectId" label="负责的工程">
          <el-select v-if="project" v-model="createForm.repos" multiple placeholder="留空表示全部工程">
            <el-option v-for="r in repoOptions" :key="r.value" :value="r.value" :label="r.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="派工工具">
          <el-select v-model="createForm.backend">
            <el-option v-for="t in modelsPage?.tools ?? []" :key="t.name" :value="t.name" :label="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="运行方式">
          <el-select v-model="createForm.mode">
            <el-option value="headless" label="headless" /><el-option value="tmux" label="tmux" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :disabled="!createForm.id.trim()" @click="createRole">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { height: 100%; min-height: 0; display: flex; flex-direction: column; }
.roles { display: grid; grid-template-columns: 240px 1fr; gap: 12px; flex: 1; min-height: 0; }
.list { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: auto; }
.list-head { display: flex; justify-content: space-between; align-items: center; font-weight: 600; margin-bottom: 8px; }
.item { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 6px 10px; margin-bottom: 6px; cursor: pointer; }
.item:hover { border-color: var(--el-color-primary-light-5); }
.item.sel { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.item .t { font-weight: 600; }
.panel { border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 14px 18px; overflow: auto; }
.sum { padding: 8px 12px; border-radius: 8px; background: var(--el-fill-color-light); margin-bottom: 10px; }
h3 { margin: 16px 0 8px; font-size: 14px; }
h4 { margin: 12px 0 6px; font-size: 13px; }
.form :deep(.el-form-item__content) { flex-wrap: wrap; gap: 4px; }
.src { margin-left: 8px; }
.pool-group { margin-bottom: 10px; }
.pool-head { margin-bottom: 4px; }
.pool-row { display: flex; align-items: center; gap: 8px; padding: 2px 0; }
.pool-row.off { opacity: .55; }
.off-note { width: 48px; }
.brief-src { margin-bottom: 6px; }
.btns { margin-top: 8px; display: flex; gap: 8px; }
.save { margin: 6px 0 4px; }
.danger { margin-top: 20px; padding-top: 12px; border-top: 1px solid var(--el-border-color); display: flex; align-items: center; gap: 10px; }
.empty { padding: 20px 4px; text-align: center; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.regs { margin-bottom: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.reg-tag { font-family: var(--el-font-family-mono, monospace); }
</style>
