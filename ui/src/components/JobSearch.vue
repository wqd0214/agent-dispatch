<script setup lang="ts">
// ⌘K 快速搜索作业：按作业名和任务文字过滤，回车跳到第一条 / 选中的作业
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSharedOverview } from '../composables/usePolling'
import { filterJobs, EMPTY_FILTERS, stateTag } from '../lib/jobs'
import { relativeTime } from '../lib/time'

const visible = defineModel<boolean>({ default: false })
const router = useRouter()
const overview = useSharedOverview()
const q = ref('')
const cursor = ref(0)

const results = computed(() => {
  if (!q.value.trim()) return []
  return filterJobs(overview.data.value?.jobs ?? [], { ...EMPTY_FILTERS, search: q.value }).slice(0, 12)
})
watch(results, () => { cursor.value = 0 })

function open(name?: string) {
  const target = name ?? results.value[cursor.value]?.name
  if (!target) return
  visible.value = false
  void router.push(`/jobs/${target}`)
}

function move(delta: number) {
  if (!results.value.length) return
  cursor.value = (cursor.value + delta + results.value.length) % results.value.length
}
</script>

<template>
  <el-dialog v-model="visible" title="搜索作业" width="560px" append-to-body>
    <el-input v-model="q" placeholder="按作业名或任务文字搜索，回车打开" autofocus clearable
      @keydown.enter.prevent="open()" @keydown.down.prevent="move(1)" @keydown.up.prevent="move(-1)" />
    <div class="results">
      <div v-for="(j, i) in results" :key="j.name" class="item" :class="{ sel: i === cursor }"
        @click="open(j.name)" @mousemove="cursor = i">
        <b>{{ j.name }}</b>
        <el-tag size="small" :type="stateTag(j).type">{{ stateTag(j).label }}</el-tag>
        <span class="sub">{{ (j.task || '').slice(0, 60) }} · {{ relativeTime(j.updated || j.created) }}</span>
      </div>
      <div v-if="q.trim() && !results.length" class="sub">没有匹配的作业</div>
    </div>
  </el-dialog>
</template>

<style scoped>
.results { margin-top: 10px; max-height: 360px; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.item { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; cursor: pointer; }
.item.sel { background: var(--el-fill-color-light); }
.item .sub { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
