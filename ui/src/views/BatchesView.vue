<script setup lang="ts">
// 事项页：左边全部事项列表（跟随顶部的项目切换过滤），右边选中事项的标题/原因（可编辑）、
// 派工者、按派出顺序排成 follows 树的全部单子。
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { batchApi } from '../api'
import { usePolling, useSharedOverview } from '../composables/usePolling'
import { useProjectScope } from '../composables/useProject'
import { followsTree } from '../lib/topics'
import { stateTag, statusTag } from '../lib/jobs'
import { relativeTime } from '../lib/time'
import type { BatchSummary } from '../types'

const route = useRoute()
const router = useRouter()
const overview = useSharedOverview()
const scope = useProjectScope(() => overview.data.value)
const batches = usePolling(() => batchApi.list(), 5000)

// 左边列表跟随项目切换过滤：事项的 jobs 里只要有当前项目的作业就显示
const visible = computed(() => {
  const names = new Set(scope.jobs.value.map(j => j.name))
  return (batches.data.value?.batches ?? []).filter(b => b.jobs.some(n => names.has(n)))
})

const selectedId = ref(typeof route.query.batch === 'string' ? route.query.batch : '')
watch(() => route.query.batch, v => { if (typeof v === 'string') selectedId.value = v })
watch(selectedId, id => {
  if ((route.query.batch || '') !== id) router.replace({ path: '/batches', query: id ? { batch: id } : {} })
})
onMounted(() => { if (!selectedId.value && visible.value.length) selectedId.value = visible.value[0].id })
watch(visible, list => { if (list.length && !list.some(b => b.id === selectedId.value)) selectedId.value = list[0].id })

const selected = computed<BatchSummary | null>(
  () => visible.value.find(b => b.id === selectedId.value)
    ?? (batches.data.value?.batches ?? []).find(b => b.id === selectedId.value) ?? null)

// 事项里的全部作业（从共享总览里取，带上状态、轮次等）
const jobsOf = computed(() => {
  const names = selected.value?.jobs ?? []
  const byName = new Map(overview.data.value?.jobs.map(j => [j.name, j]) ?? [])
  return names.map(n => byName.get(n)).filter((j): j is NonNullable<typeof j> => !!j)
})
const tree = computed(() => followsTree(jobsOf.value))

const edit = reactive({ open: false, title: '', reason: '', saving: false })
watch(selected, b => { edit.open = false; edit.title = b?.title ?? ''; edit.reason = b?.reason ?? '' })
async function save() {
  if (!selected.value) return
  edit.saving = true
  try {
    await batchApi.update(selected.value.id, { title: edit.title, reason: edit.reason })
    ElMessage.success('已保存')
    edit.open = false
    batches.refresh()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : String(e))
  } finally {
    edit.saving = false
  }
}
function tagOf(j: { state: string; state_label: string; needs_user: string | null; waiting_models?: { backend: string; id: string; alias: string | null }[] }) { return stateTag(j) }
</script>

<template>
  <div class="batches">
    <div class="list">
      <div class="count sub">共 {{ visible.length }} 个事项</div>
      <div v-if="!visible.length" class="sub empty">还没有事项。派工时用 --batch 归组，并带 --batch-title 说明在办什么事</div>
      <div v-for="b in visible" :key="b.id" class="item" :class="{ sel: b.id === selectedId }" @click="selectedId = b.id">
        <div class="row">
          <b class="title">{{ b.title || b.id }}</b>
          <span v-if="!b.title" class="unnamed">未命名</span>
        </div>
        <div class="sub">
          {{ b.dispatcher.kind }}{{ b.dispatcher.title ? ` · ${b.dispatcher.title}` : '' }} ·
          {{ b.counts.total }} 张 · {{ relativeTime(b.activity) }}
        </div>
        <div class="sub">
          <span class="run" v-if="b.counts.running">执行中 {{ b.counts.running }}</span>
          <span class="rev" v-if="b.counts.review">待审查 {{ b.counts.review }}</span>
          <span class="ok" v-if="b.counts.done">已完成 {{ b.counts.done }}</span>
          <span class="bad" v-if="b.counts.failed">失败 {{ b.counts.failed }}</span>
        </div>
      </div>
    </div>
    <div class="detail">
      <template v-if="selected">
        <div class="head">
          <b class="title">{{ selected.title || `（未命名）${selected.id}` }}</b>
          <el-tag size="small" type="info" effect="plain">批次 {{ selected.id }}</el-tag>
          <el-button v-if="!edit.open" link type="primary" size="small" @click="edit.open = true">编辑</el-button>
        </div>
        <template v-if="!edit.open">
          <div class="sub meta">
            {{ selected.reason || '（没有写原因）' }}<br>
            派工者：{{ selected.dispatcher.kind }}{{ selected.dispatcher.title ? ` · ${selected.dispatcher.title}` : '' }}
            · 最近活动 {{ relativeTime(selected.activity) }}
          </div>
        </template>
        <div v-else class="edit">
          <el-input v-model="edit.title" placeholder="事项标题" size="small" />
          <el-input v-model="edit.reason" type="textarea" :rows="2" placeholder="为什么派这批单" size="small" />
          <div>
            <el-button size="small" :loading="edit.saving" @click="edit.open = false">取消</el-button>
            <el-button size="small" type="primary" :loading="edit.saving" @click="save">保存</el-button>
          </div>
        </div>

        <div class="jobs">
          <div v-for="node in tree" :key="node.job.name" class="job" :style="{ marginLeft: `${node.depth * 18}px` }"
            @click="router.push(`/jobs/${node.job.name}`)">
            <div class="row">
              <b class="name">{{ node.job.name }}</b>
              <el-tag size="small" :type="tagOf(node.job).type">{{ tagOf(node.job).label }}</el-tag>
              <el-tag v-if="statusTag(node.job.status)" size="small" :type="statusTag(node.job.status)!.type" effect="plain">
                {{ node.job.status }}
              </el-tag>
            </div>
            <div class="sub">
              {{ node.job.role_title || node.job.role || '-' }} · 第 {{ node.job.turn }} 轮 ·
              {{ node.job.seconds == null ? '-' : `${Math.round(node.job.seconds / 60)} 分钟` }}
              <template v-if="node.follows.length"> · 因 {{ node.follows.join('、') }} 而派</template>
            </div>
          </div>
          <div v-if="!tree.length" class="sub">（这个事项还没有作业记录）</div>
        </div>
      </template>
      <el-empty v-else description="从左边选择一个事项" />
    </div>
  </div>
</template>

<style scoped>
.batches { display: grid; grid-template-columns: 340px 1fr; gap: 12px; height: 100%; min-height: 0; }
.list, .detail { display: flex; flex-direction: column; min-height: 0; border: 1px solid var(--el-border-color); border-radius: 8px; background: var(--el-bg-color); padding: 10px; overflow: hidden; }
.count { padding: 2px 2px 6px; }
.empty { padding: 20px 4px; }
.item { border: 1px solid var(--el-border-color); border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer; }
.item:hover { border-color: var(--el-color-primary-light-5); }
.item.sel { border-color: var(--el-color-primary); box-shadow: 0 0 0 2px var(--el-color-primary-light-8); }
.item .row { display: flex; align-items: center; gap: 8px; }
.title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.unnamed { font-size: 11px; color: var(--el-text-color-secondary); border: 1px dashed var(--el-border-color); border-radius: 8px; padding: 0 6px; }
.run { color: var(--el-color-primary); margin-right: 6px; }
.rev { color: var(--el-color-warning); margin-right: 6px; }
.ok { color: var(--el-color-success); margin-right: 6px; }
.bad { color: var(--el-color-danger); }
.detail { overflow: auto; }
.head { display: flex; align-items: center; gap: 8px; }
.head .title { font-size: 16px; }
.meta { margin: 4px 0 10px; line-height: 1.7; }
.edit { display: flex; flex-direction: column; gap: 8px; margin-bottom: 10px; align-items: flex-start; }
.edit .el-input, .edit .el-textarea { width: 100%; }
.jobs { display: flex; flex-direction: column; gap: 6px; }
.job { border: 1px solid var(--el-border-color-lighter); border-radius: 8px; padding: 6px 10px; cursor: pointer; }
.job:hover { border-color: var(--el-color-primary-light-5); }
.job .row { display: flex; align-items: center; gap: 8px; }
.job .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
