<script setup lang="ts">
// 全局「工具与模型」页：派工工具启停、模型登记表（导入 / 手工登记 / 启停 / 备注 / 并发上限 / 删除）。
// 各角色的模型池在角色页里编辑，这里只展示「用在哪些角色」并可点过去。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { modelApi } from '../api'
import type { ModelCandidate, ModelPage } from '../types'
import { useCurrentProject } from '../composables/useProject'

const router = useRouter()
const { set: setProject } = useCurrentProject()
const page = ref<ModelPage | null>(null)
const loading = ref(false)
const filters = reactive({ text: '', backend: '', used: false })
const addForm = reactive({ alias: '', backend: '', id: '', note: '' })
const defaultLimit = ref<number>(5)
// 导入对话框：先看候选（已登记 / 未登记），可搜索勾选，确认后只导入勾选的
const importDlg = reactive({ open: false, backend: '', loading: false, query: '', checked: [] as string[], candidates: [] as ModelCandidate[], error: '' })

function msg(e: unknown): string { return e instanceof Error ? e.message : String(e) }
// 「用在哪些角色」的标签文字：项目 · 角色 ×比例，不是最高档的再标优先级
function usedByLabel(u: ModelPage['models'][number]['used_by'][number]): string {
  const share = `×${u.weight}${(u.priority || 1) > 1 ? ` 优先级${u.priority}` : ''}`
  return u.project ? `${u.project_name || u.project} · ${u.role_title} ${share}` : `${u.role_title} ${share}`
}
async function reload() {
  loading.value = true
  try {
    page.value = await modelApi.page()
    defaultLimit.value = page.value.default_max_concurrent ?? 5
  } catch (e) { ElMessage.error(msg(e)) } finally { loading.value = false }
}
onMounted(reload)

async function act(fn: () => Promise<unknown>, okText?: string) {
  try { await fn(); if (okText) ElMessage.success(okText); await reload() } catch (e) { ElMessage.error(msg(e)) }
}

const filtered = computed(() => {
  const text = filters.text.trim().toLowerCase()
  return (page.value?.models ?? []).filter(m =>
    (!filters.backend || m.backend === filters.backend)
    && (!filters.used || m.used_by.length || m.jobs)
    && (!text || `${m.alias} ${m.id} ${m.note}`.toLowerCase().includes(text)))
})

function stateText(m: ModelPage['models'][number]): { text: string; tone: 'success' | 'danger' | 'warning' | 'info' } {
  if (!m.enabled) return { text: '停用', tone: 'info' }
  if (!m.backend_enabled) return { text: `${m.backend} 已停用`, tone: 'danger' }
  if (m.paused) return { text: '暂停中', tone: 'warning' }
  return { text: '可用', tone: 'success' }
}
const pct = (a: number, b: number) => (b ? `${Math.round((a * 100) / b)}%` : '-')
function duration(seconds: number | null): string {
  if (!seconds) return '-'
  const m = Math.round(seconds / 60)
  return m >= 60 ? `${Math.floor(m / 60)}时${m % 60}分` : `${m}分`
}
async function del(m: ModelPage['models'][number]) {
  const { ElMessageBox } = await import('element-plus')
  try { await ElMessageBox.confirm(`从登记表删除 ${m.alias}？`, '二次确认', { type: 'warning' }) } catch { return }
  await act(async () => { await modelApi.remove(m.alias) }, '已删除')
}
const importCandidates = computed(() => {
  const q = importDlg.query.trim().toLowerCase()
  return importDlg.candidates.filter(c => !q || `${c.backend} ${c.id} ${c.alias ?? ''}`.toLowerCase().includes(q))
})
const importSelectable = computed(() => importCandidates.value.filter(c => !c.registered).map(c => c.id))
async function openImport(backend: string) {
  importDlg.open = true
  importDlg.backend = backend
  importDlg.loading = true
  importDlg.query = ''
  importDlg.checked = []
  importDlg.candidates = []
  importDlg.error = ''
  try { importDlg.candidates = (await modelApi.candidates(backend)).candidates } catch (e) { importDlg.error = msg(e) } finally { importDlg.loading = false }
}
function toggleAllImport() {
  const all = importSelectable.value
  importDlg.checked = all.every(id => importDlg.checked.includes(id)) ? [] : all
}
async function doImport() {
  const names = [...importDlg.checked]
  importDlg.open = false
  if (!names.length) return
  try {
    const r = await modelApi.importNames(names, importDlg.backend || undefined)
    ElMessage.success(`新登记 ${r.added.length} 个模型` + (r.added.length ? `：${r.added.join('、')}` : ''))
    await reload()
  } catch (e) { ElMessage.error(msg(e)) }
}
async function saveDefaultLimit(value: number | undefined) {
  if (!value || value < 1) { defaultLimit.value = page.value?.default_max_concurrent ?? 5; return }
  await act(async () => { await modelApi.setDefaultLimit(value) }, `全局默认并发上限已设为 ${value}`)
}
async function saveLimit(alias: string, value: string) {
  const text = value.trim()
  if (!text) {  // 留空恢复默认
    await act(async () => { await modelApi.update(alias, { max_concurrent: null }) }, '已恢复默认上限')
    return
  }
  const n = Number(text)
  if (!Number.isInteger(n) || n < 1) { ElMessage.error('上限必须是 ≥1 的整数'); await reload(); return }
  await act(async () => { await modelApi.update(alias, { max_concurrent: n }) }, '上限已保存')
}
async function add() {
  await act(async () => { await modelApi.add({ ...addForm }) }, '已添加')
  Object.assign(addForm, { alias: '', id: '', note: '' })
}
function gotoRole(project: string, role: string) {
  setProject(project)
  void router.push({ path: '/settings/roles', query: { role } })
}
</script>

<template>
  <div class="models" v-loading="loading">
    <h2>派工工具</h2>
    <div class="sub">停用后：新派工不会再用它，模型池里属于它的模型自动跳过；进行中的作业不受影响</div>
    <el-table :data="page?.tools ?? []" size="small" class="tbl">
      <el-table-column prop="name" label="工具" width="120" />
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'danger'" size="small">{{ row.enabled ? '可用' : '已停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="default_model" label="默认模型" width="200" />
      <el-table-column prop="models" label="登记的模型" width="110" />
      <el-table-column>
        <template #default="{ row }">
          <el-button size="small" :type="row.enabled ? 'danger' : 'primary'" plain
            @click="act(async () => { await modelApi.setBackendEnabled(row.name, !row.enabled) }, row.enabled ? `已停用 ${row.name}` : `已启用 ${row.name}`)">
            {{ row.enabled ? '停用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <h2>模型登记表</h2>
    <div class="sub">{{ page?.models.length ?? 0 }} 个模型 · 服务商报错后暂停 {{ page?.cooldown_min }} 分钟</div>
    <div class="toolbar">
      <span>全局默认并发上限</span>
      <el-input-number v-model="defaultLimit" :min="1" size="small" style="width: 110px" @change="saveDefaultLimit" />
      <span class="sub">同一个模型同时最多承接这么多张派工单，超出的会排队</span>
    </div>
    <div class="toolbar">
      <el-button v-for="b in page?.backends ?? []" :key="b" size="small" @click="openImport(b)">从 {{ b }} 导入</el-button>
      <span class="sub">或手动登记：</span>
      <el-input v-model="addForm.alias" placeholder="短名，如 pi:glm-flash" size="small" style="width: 170px" />
      <el-select v-model="addForm.backend" size="small" style="width: 110px" placeholder="工具">
        <el-option v-for="b in page?.backends ?? []" :key="b" :value="b" :label="b" />
      </el-select>
      <el-input v-model="addForm.id" placeholder="后端里的模型 ID，如 provider/model" size="small" style="width: 280px" />
      <el-input v-model="addForm.note" placeholder="备注（可选）" size="small" style="width: 140px" />
      <el-button size="small" type="primary" :disabled="!addForm.alias.trim() || !addForm.id.trim()" @click="add">登记</el-button>
    </div>
    <div class="toolbar">
      <el-input v-model="filters.text" placeholder="搜索短名、模型 ID、备注" clearable size="small" style="width: 240px" />
      <el-select v-model="filters.backend" clearable placeholder="全部工具" size="small" style="width: 130px">
        <el-option v-for="b in page?.backends ?? []" :key="b" :value="b" :label="b" />
      </el-select>
      <el-checkbox v-model="filters.used" size="small">只看用在角色里或派过工的</el-checkbox>
      <span class="sub">显示 {{ filtered.length }} / {{ page?.models.length ?? 0 }}</span>
    </div>
    <el-table :data="filtered" size="small" class="tbl">
      <el-table-column label="模型" min-width="200">
        <template #default="{ row }">
          <b>{{ row.alias }}</b>
          <div class="sub mono">{{ row.id }}</div>
        </template>
      </el-table-column>
      <el-table-column prop="backend" label="工具" width="100" />
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag :type="stateText(row).tone" size="small">{{ stateText(row).text }}</el-tag>
          <el-tooltip v-if="row.missing"
            :content="`工具里已经列不出这个模型${row.used_by.length ? '，但仍被 ' + row.used_by.map((u: ModelPage['models'][number]['used_by'][number]) => u.role_title).join('、') + ' 的模型池使用' : ''}`">
            <el-tag type="danger" size="small">工具里已不存在</el-tag>
          </el-tooltip>
          <div v-if="row.paused" class="sub" :title="row.paused.reason">
            至 {{ new Date(row.paused.until * 1000).toLocaleTimeString() }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="并发（占用 / 上限）" width="160">
        <template #default="{ row }">
          <span :class="{ full: row.running >= row.limit }">{{ row.running }}</span>
          <span v-if="row.running >= row.limit" class="full-tag">/{{ row.limit }} 满</span>
          <span v-else>/{{ row.limit }}</span>
          <el-tooltip content="留空恢复全局默认值">
            <el-input class="limit-input" :model-value="row.limit_source === 'model' ? String(row.limit) : ''"
              :placeholder="`默认 ${page?.default_max_concurrent ?? 5}`" size="small"
              @change="(v: string) => saveLimit(row.alias, v)" />
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="用在哪些角色" min-width="220">
        <template #default="{ row }">
          <template v-if="row.used_by.length">
            <el-button v-for="u in row.used_by.slice(0, 3)" :key="u.project + u.role" link size="small"
              @click="gotoRole(u.project, u.role)">
              {{ usedByLabel(u) }}
            </el-button>
            <el-tooltip v-if="row.used_by.length > 3" placement="top"
              :content="row.used_by.map((u: ModelPage['models'][number]['used_by'][number]) => usedByLabel(u)).join('、')">
              <span class="more">等 {{ row.used_by.length }} 个</span>
            </el-tooltip>
          </template>
          <span v-else class="sub">-</span>
        </template>
      </el-table-column>
      <el-table-column prop="jobs" label="作业" width="70" />
      <el-table-column label="通过率" width="80">
        <template #default="{ row }">{{ pct(row.pass, row.settled) }}</template>
      </el-table-column>
      <el-table-column label="平均耗时" width="90">
        <template #default="{ row }">{{ duration(row.avg_seconds) }}</template>
      </el-table-column>
      <el-table-column label="服务商报错" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.provider_failures" type="danger" size="small">{{ row.provider_failures }}</el-tag>
          <span v-else class="sub">0</span>
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="200">
        <template #default="{ row }">
          <el-input :model-value="row.note" size="small" placeholder="备注"
            @change="(v: string) => act(async () => { await modelApi.update(row.alias, { note: v }) }, '备注已保存')" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.paused" link size="small" type="primary"
            @click="act(async () => { await modelApi.resume(row.alias) }, '已恢复')">恢复</el-button>
          <el-button link size="small"
            @click="act(async () => { await modelApi.update(row.alias, { enabled: !row.enabled }) }, row.enabled ? '已停用' : '已启用')">
            {{ row.enabled ? '停用' : '启用' }}
          </el-button>
          <el-button link size="small" type="danger" @click="del(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="page && !page.models.length"
      description="还没有登记模型。点上面的「从 pi 导入」等按钮，把这个工具里配置的模型登记进来。" />

    <el-dialog v-model="importDlg.open" :title="`从 ${importDlg.backend} 导入模型`" width="640px" v-loading="importDlg.loading">
      <el-alert v-if="importDlg.error" type="error" :closable="false" show-icon :title="importDlg.error" style="margin-bottom: 8px" />
      <div class="toolbar">
        <el-input v-model="importDlg.query" placeholder="搜索模型" clearable size="small" style="width: 240px" />
        <el-button size="small" @click="toggleAllImport">全选未登记</el-button>
        <span class="sub">已选 {{ importDlg.checked.length }} · 未登记 {{ importDlg.candidates.filter(c => !c.registered).length }} 个</span>
      </div>
      <el-checkbox-group v-model="importDlg.checked" class="cand-list">
        <div v-for="c in importCandidates" :key="c.backend + c.id" class="cand" :class="{ off: c.registered }">
          <el-checkbox :value="c.id" :disabled="c.registered">
            <span class="mono">{{ c.id }}</span>
          </el-checkbox>
          <el-tag v-if="c.registered" type="info" size="small">已登记（{{ c.alias }}）</el-tag>
          <el-tag v-else size="small">未登记</el-tag>
        </div>
      </el-checkbox-group>
      <el-empty v-if="!importDlg.loading && !importDlg.candidates.length"
        :description="importDlg.error || '没有找到候选'" :image-size="60" />
      <div class="sub" style="margin-top: 8px">只导入勾选的；已登记的不会重复导入</div>
      <template #footer>
        <el-button @click="importDlg.open = false">取消</el-button>
        <el-button type="primary" :disabled="!importDlg.checked.length" @click="doImport">导入 {{ importDlg.checked.length }} 个</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
h2 { font-size: 15px; margin: 14px 0 4px; }
.tbl { margin: 8px 0 4px; }
.toolbar { display: flex; align-items: center; gap: 8px; margin: 6px 0; flex-wrap: wrap; }
.sub { color: var(--el-text-color-secondary); font-size: 12px; }
.more { color: var(--el-color-primary); font-size: 12px; cursor: default; white-space: nowrap; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
.full { color: var(--el-color-danger); font-weight: 600; }
.full-tag { color: var(--el-color-danger); }
.limit-input { width: 90px; display: inline-block; margin-left: 6px; }
.cand-list { max-height: 380px; overflow: auto; }
.cand { display: flex; align-items: center; gap: 8px; padding: 1px 0; }
.cand.off { opacity: 0.6; }
</style>
