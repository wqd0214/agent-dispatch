import { createRouter, createWebHistory } from 'vue-router'
import RolesView from './views/RolesView.vue' // 角色页常用，直接打包避免懒加载自闪

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
    { path: '/dashboard', redirect: '/' },
    {
      path: '/jobs/:name?',
      name: 'jobs',
      component: () => import('./views/JobsView.vue'),
    },
    { path: '/batches', name: 'batches', component: () => import('./views/BatchesView.vue') },
    { path: '/stats', component: () => import('./views/Placeholder.vue'), props: { title: '统计' } },
    { path: '/settings/roles', component: RolesView },
    { path: '/settings/repos', component: () => import('./views/ReposView.vue') },
    { path: '/settings/mcp', component: () => import('./views/McpView.vue') },
    { path: '/settings/skills', component: () => import('./views/SkillsView.vue') },
    { path: '/settings/supervisor', component: () => import('./views/SupervisorView.vue') },
    { path: '/global/models', component: () => import('./views/GlobalModelsView.vue') },
    { path: '/settings/:page', component: () => import('./views/Placeholder.vue'), props: route => ({ title: '项目设置' }) },
  ],
})
