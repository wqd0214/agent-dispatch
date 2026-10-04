<script setup lang="ts">
defineProps<{ title: string }>()
</script>

<template>
  <el-empty :description="`${title}：即将提供`" />
</template>
