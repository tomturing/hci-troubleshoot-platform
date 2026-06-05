import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  // base 与 vite.config.ts 的 base 保持一致（挂载在 /admin/ 子路径）
  history: createWebHistory('/admin/'),
  routes: [
    {
      path: '/',
      redirect: '/dashboard',
    },
    {
      path: '/dashboard',
      name: 'Dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { title: '仪表盘', icon: 'Odometer', order: 1 },
    },
    {
      path: '/observability',
      name: 'Observability',
      component: () => import('@/views/ObservabilityView.vue'),
      meta: { title: '可观测性', icon: 'Monitor', order: 2 },
    },
    {
      path: '/clients',
      name: 'Clients',
      component: () => import('@/views/ClientListView.vue'),
      meta: { title: '用户管理', icon: 'User', order: 3 },
    },
    {
      path: '/cases',
      name: 'Cases',
      component: () => import('@/views/CaseListView.vue'),
      meta: { title: '工单管理', icon: 'Tickets', order: 4 },
    },
    {
      path: '/cases/:caseId',
      name: 'CaseDetail',
      component: () => import('@/views/CaseDetailView.vue'),
      meta: { title: '工单详情', hidden: true },
    },
    {
      path: '/category',
      name: 'CategoryManage',
      component: () => import('@/views/CategoryManageView.vue'),
      meta: { title: '分类基线', icon: 'Histogram', order: 5 },
    },
    {
      path: '/knowledge/kbd-review',
      name: 'KbdReview',
      component: () => import('@/views/KbdReviewView.vue'),
      meta: { title: 'KBD管理', icon: 'Document', order: 6 },
    },
    {
      path: '/knowledge/sop',
      name: 'SopManage',
      component: () => import('@/views/SopManageView.vue'),
      meta: { title: 'SOP管理', icon: 'Notebook', order: 7 },
    },
    {
      path: '/tools',
      name: 'ToolManage',
      component: () => import('@/views/ToolManageView.vue'),
      meta: { title: '工具管理', icon: 'Setting', order: 8 },
    },
    {
      path: '/skills',
      name: 'SkillManage',
      component: () => import('@/views/SkillManageView.vue'),
      meta: { title: '技能管理', icon: 'Briefcase', order: 9 },
    },
    {
      path: '/prompts',
      name: 'PromptManage',
      component: () => import('@/views/PromptManageView.vue'),
      meta: { title: 'Prompt管理', icon: 'Cpu', order: 10 },
    },
  ],
})

export default router
