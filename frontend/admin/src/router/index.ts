import { createRouter, createWebHistory } from 'vue-router'
import { isAuthenticated } from '@/utils/auth'

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
      meta: { title: '可观测性', icon: 'DataAnalysis', order: 4 },
    },
    {
      path: '/simulation',
      name: 'SimulationTest',
      component: () => import('@/views/SimulationTestView.vue'),
      meta: { title: '仿真测试', icon: 'VideoPlay', order: 12 },
    },
    {
      path: '/simulation/bundle-factory',
      name: 'BundleFactory',
      component: () => import('@/views/BundleFactoryView.vue'),
      meta: { title: 'Bundle工厂', icon: 'Box', order: 12.5 },
    },
    {
      path: '/simulation/bundle-factory/assets',
      name: 'BundleFixtureAssets',
      component: () => import('@/views/BundleFixtureAssetsView.vue'),
      meta: { title: '模板实例库', icon: 'Files', order: 12.6 },
    },
    {
      path: '/clients',
      name: 'Clients',
      component: () => import('@/views/ClientListView.vue'),
      meta: { title: '用户管理', icon: 'User', order: 2 },
    },
    {
      path: '/cases',
      name: 'Cases',
      component: () => import('@/views/CaseListView.vue'),
      meta: { title: '工单管理', icon: 'Tickets', order: 3 },
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
      path: '/catalog',
      name: 'CatalogManage',
      component: () => import('@/views/CatalogManageView.vue'),
      meta: { title: 'Catalog基线', icon: 'Collection', order: 5.5 },
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
    {
      path: '/offline-diagnosis',
      name: 'OfflineDiagnosis',
      component: () => import('@/views/OfflineDiagnosisView.vue'),
      meta: { title: '离线诊断', icon: 'FirstAidKit', order: 11 },
    },
    {
      path: '/vm-console-audit',
      name: 'VmConsoleAudit',
      component: () => import('@/views/VmConsoleCaptureView.vue'),
      meta: { title: 'Console审计', icon: 'Monitor', order: 11.5 },
    },
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
  ],
})

// 路由守卫（L3 强制登录）：
// - 公开路由（meta.public，如 /login）无需登录；已登录访问 /login 自动跳工作台避免重复登录。
// - 受保护路由：未登录或令牌过期 → 强制跳 /login 并携带 redirect 回跳地址，登录成功后回跳。
// 令牌真伪以服务端 JWKS 验签为准，isAuthenticated 的过期判断仅用于提前拦截、改善体验。
router.beforeEach((to: any) => {
  if (to.meta?.public) {
    if (to.path === '/login' && isAuthenticated()) {
      return { path: '/dashboard' }
    }
    return true
  }
  if (!isAuthenticated()) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
