"""
HCI 智能排障平台 — Locust 负载测试基线

使用方式：
  # 交互式 UI 模式
  locust -f tests/load/locustfile.py --host http://localhost:8000

  # 无头批量模式（建立基线快照）
  locust -f tests/load/locustfile.py \
    --host http://localhost:8000 \
    --headless -u 50 -r 5 -t 60s \
    --csv tests/load/baseline_$(date +%Y%m%d)

基线目标（P95）：
  - REST API（工单 CRUD / 健康检查）：P95 < 200ms
  - AI 对话流（SSE message）：P95 < 2000ms

环境变量：
  LOAD_TEST_HOST        - 目标主机（默认 http://localhost:8000）
  LOAD_TEST_TOKEN       - 内部 Token（默认 hci-dev-internal-token）
  LOAD_TEST_WAIT_MIN    - 最小等待时间 ms（默认 500）
  LOAD_TEST_WAIT_MAX    - 最大等待时间 ms（默认 2000）
"""

import os
import random

from locust import HttpUser, TaskSet, between, events, task
from locust.exception import RescheduleTask

# ──────────────────────────────────────────────────────────────────────────────
# 全局配置
# ──────────────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("LOAD_TEST_TOKEN", "hci-dev-internal-token")
WAIT_MIN = float(os.environ.get("LOAD_TEST_WAIT_MIN", "500")) / 1000
WAIT_MAX = float(os.environ.get("LOAD_TEST_WAIT_MAX", "2000")) / 1000

COMMON_HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# 测试工单标题样本
CASE_TITLES = [
    "K8s 节点 NotReady",
    "虚拟机电源故障",
    "网络连接超时",
    "Pod 启动失败",
    "磁盘 IO 高负载",
    "内存 OOM 告警",
    "SSL 证书过期",
    "数据库连接池耗尽",
]

# 测试对话问题样本
USER_QUESTIONS = [
    "帮我分析一下这个故障的可能原因",
    "节点 NotReady 通常是什么原因导致的？",
    "有哪些排查步骤可以参考？",
    "这个问题在生产环境常见吗？",
    "如何避免这类问题再次发生？",
]


# ──────────────────────────────────────────────────────────────────────────────
# TaskSet：REST API 场景（工单 CRUD + 健康检查）
# ──────────────────────────────────────────────────────────────────────────────


class RestAPITasks(TaskSet):
    """REST API 任务集 — 模拟工单管理操作"""

    case_ids: list[str] = []  # 跨请求共享的已创建工单 ID

    def on_start(self):
        """用户启动时创建一个初始工单"""
        self._create_case()

    @task(5)
    def health_check(self):
        """健康检查（高频、轻量）"""
        with self.client.get(
            "/api/health",
            headers=COMMON_HEADERS,
            name="GET /api/health",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"健康检查失败：{resp.status_code}")

    @task(3)
    def list_cases(self):
        """查询工单列表"""
        with self.client.get(
            "/api/cases/?limit=20",
            headers=COMMON_HEADERS,
            name="GET /api/cases [list]",
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"工单列表查询失败：{resp.status_code}")

    @task(2)
    def create_case(self):
        """创建新工单"""
        self._create_case()

    @task(2)
    def get_case_detail(self):
        """获取工单详情"""
        if not self.case_ids:
            raise RescheduleTask()
        case_id = random.choice(self.case_ids)
        with self.client.get(
            f"/api/cases/{case_id}",
            headers=COMMON_HEADERS,
            name="GET /api/cases/:id [detail]",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"工单详情查询失败：{resp.status_code}")

    @task(1)
    def close_case(self):
        """关闭工单（低频，防止耗尽工单池）"""
        if len(self.case_ids) < 3:
            raise RescheduleTask()
        case_id = self.case_ids.pop(0)
        with self.client.patch(
            f"/api/cases/{case_id}",
            json={"status": "closed"},
            headers=COMMON_HEADERS,
            name="PATCH /api/cases/:id [close]",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"关闭工单失败：{resp.status_code}")

    def _create_case(self):
        """内部：创建工单并缓存 case_id"""
        with self.client.post(
            "/api/cases/",
            json={
                "client_id": f"load-user-{random.randint(1000, 9999)}",
                "title": random.choice(CASE_TITLES),
                "description": "负载测试自动创建，请忽略",
                "source": "load_test",
            },
            headers=COMMON_HEADERS,
            name="POST /api/cases [create]",
            catch_response=True,
        ) as resp:
            if resp.status_code == 201:
                try:
                    data = resp.json()
                    if case_id := data.get("case_id"):
                        self.case_ids.append(case_id)
                        if len(self.case_ids) > 20:
                            self.case_ids = self.case_ids[-20:]
                except Exception:
                    pass
            else:
                resp.failure(f"创建工单失败：{resp.status_code}")


# ──────────────────────────────────────────────────────────────────────────────
# TaskSet：KB Service 知识库查询场景
# ──────────────────────────────────────────────────────────────────────────────


class KBSearchTasks(TaskSet):
    """知识库检索任务集 — 模拟 AI 辅助检索 + 知识查询"""

    QUERIES = [
        "虚拟机无法启动",
        "K8s Pod CrashLoopBackOff",
        "内存泄漏排查",
        "网络丢包故障",
        "磁盘写满处理",
        "SSL 握手失败",
    ]

    @task(3)
    def search_kb(self):
        """知识库语义检索"""
        with self.client.post(
            "/api/kb/search",
            json={
                "query": random.choice(self.QUERIES),
                "top_n": 3,
                "score_threshold": 0.3,
            },
            headers=COMMON_HEADERS,
            name="POST /api/kb/search",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"KB 检索失败：{resp.status_code}")

    @task(1)
    def kb_stats(self):
        """KB 统计接口"""
        with self.client.get(
            "/api/kb/stats",
            headers=COMMON_HEADERS,
            name="GET /api/kb/stats",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 404):
                resp.failure(f"KB stats 失败：{resp.status_code}")


# ──────────────────────────────────────────────────────────────────────────────
# 用户类型定义
# ──────────────────────────────────────────────────────────────────────────────


class CaseworkerUser(HttpUser):
    """工单处理员 — 主要执行 REST CRUD 操作"""
    tasks = [RestAPITasks]
    wait_time = between(WAIT_MIN, WAIT_MAX)
    weight = 7  # 70% 流量


class KBAnalystUser(HttpUser):
    """知识库分析员 — 主要执行 KB 检索操作"""
    tasks = [KBSearchTasks]
    wait_time = between(WAIT_MIN * 2, WAIT_MAX * 2)
    weight = 3  # 30% 流量


# ──────────────────────────────────────────────────────────────────────────────
# 基线捕获钩子
# ──────────────────────────────────────────────────────────────────────────────


@events.quitting.add_listener
def check_baseline(environment, **kwargs):
    """
    测试结束时检查是否满足 P95 基线目标：
    - REST API P95 < 200ms
    - KB 检索 P95 < 1000ms

    超标时以非零退出码退出（CI 中可触发 Fail）
    """
    stats = environment.stats

    failures = []
    for name, entry in stats.entries.items():
        method, endpoint = name
        p95 = entry.get_response_time_percentile(0.95)

        if p95 is None:
            continue

        # AI 对话 / 流式接口宽松阈值
        if "message" in endpoint or "conversation" in endpoint:
            if p95 > 2000:
                failures.append(f"[{method} {endpoint}] P95={p95:.0f}ms > 2000ms (AI 对话基线)")
        else:
            # REST 接口严格阈值
            if p95 > 200:
                failures.append(f"[{method} {endpoint}] P95={p95:.0f}ms > 200ms (REST 基线)")

    if failures:
        print("\n⚠️  以下接口超过 P95 基线：")
        for f in failures:
            print(f"  - {f}")
        environment.process_exit_code = 1
    else:
        print("\n✅ 所有接口 P95 均在基线范围内")
