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
      path: '/cases',
      name: 'Cases',
      component: () => import('@/views/CaseListView.vue'),
      meta: { title: '工单管理', icon: 'Tickets', order: 2 },
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
      meta: { title: '分类基线', icon: 'Histogram', order: 3 },
    },
    {
      path: '/knowledge/kbd-review',
      name: 'KbdReview',
      component: () => import('@/views/KbdReviewView.vue'),
      meta: { title: 'KBD 审核', icon: 'Document', order: 4 },
    },
    {
      path: '/clients',
      name: 'Clients',
      component: () => import('@/views/ClientListView.vue'),
      meta: { title: '用户管理', icon: 'User', order: 5 },
    },
    {
      path: '/monitoring',
      name: 'Monitoring',
      component: () => import('@/views/MonitoringView.vue'),
      meta: { title: '系统监控', icon: 'Monitor', order: 6 },
    },
  ],
})

export default router
