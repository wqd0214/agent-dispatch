import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/jobs' },
    {
      path: '/jobs/:name?',
      name: 'jobs',
      component: () => import('./views/JobsView.vue'),
    },
    { path: '/dashboard', component: () => import('./views/Placeholder.vue'), props: { title: '工作台' } },
    { path: '/batches', component: () => import('./views/Placeholder.vue'), props: { title: '批次' } },
    { path: '/stats', component: () => import('./views/Placeholder.vue'), props: { title: '统计' } },
    { path: '/settings/:page', component: () => import('./views/Placeholder.vue'), props: route => ({ title: ({ repos: '工程', roles: '角色', mcp: 'MCP', skills: '技能', supervisor: '主管规则' } as Record<string, string>)[String(route.params.page)] || '项目设置' }) },
  ],
})
