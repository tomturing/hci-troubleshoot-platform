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
      meta: { title: '仪表盘', icon: 'Odometer' },
    },
    {
      path: '/cases',
      name: 'Cases',
      component: () => import('@/views/CaseListView.vue'),
      meta: { title: '工单管理', icon: 'Tickets' },
    },
    {
      path: '/cases/:caseId',
      name: 'CaseDetail',
      component: () => import('@/views/CaseDetailView.vue'),
      meta: { title: '工单详情', hidden: true },
    },
    {
      path: '/clients',
      name: 'Clients',
      component: () => import('@/views/ClientListView.vue'),
      meta: { title: '用户管理', icon: 'User' },
    },
    {
      path: '/monitoring',
      name: 'Monitoring',
      component: () => import('@/views/MonitoringView.vue'),
      meta: { title: '系统监控', icon: 'Monitor' },
    },
    {
      path: '/knowledge/review',
      name: 'KnowledgeReview',
      component: () => import('@/views/KnowledgeReviewView.vue'),
      meta: { title: '知识审核', icon: 'Reading' },
    },
  ],
})

export default router
