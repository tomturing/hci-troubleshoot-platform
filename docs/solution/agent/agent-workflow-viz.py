"""
Agent 工作过程可视化 — Streamlit 交互演示

用法:
    cd docs/solution/agent
    streamlit run agent-workflow-viz.py
"""

import streamlit as st
import json
import time

st.set_page_config(page_title="Agent 工作过程可视化", layout="wide")
st.title("Agent 工作过程可视化")

# ============================================================
# Data: SOP Trace from §3.2
# ============================================================
TRACE = [
    {"type":"system","content":"用户报告: api-gateway 服务大量 502 错误，日志出现 'connection timeout'","step":0},
    {"type":"s0_triage","content":"S0 分类: database (置信度 0.94)\n匹配 SOP: sop-db-pool-exhaustion\n原因: 关键词 connection timeout + 502 匹配数据库连接池故障模式","step":0},
    {"type":"thought","content":"S0 已将工单分类为 database 并匹配到 SOP。需要先获取根节点了解排查入口。","step":1},
    {"type":"tool_call","name":"get_sop_node","args":{"node_id":"n-1"},"step":1,"category":"sop"},
    {"type":"tool_result","name":"get_sop_node","content":"返回根节点 n-1:\n• 3 个子分支 (n-1-1/n-1-2/n-1-3)\n• required_variables: service_name, node_ip, db_pool_status\n• preferred_next_steps: 先获取 service_name","step":1,"category":"sop"},
    {"type":"thought","content":"根节点有三个子分支。preferred_next_steps 提示先获取 service_name。需要向用户询问。","step":2},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"service_name"},"step":2,"category":"sop"},
    {"type":"tool_result","name":"sop_request_variable","content":"needs_input=true → 暂停，等待用户输入服务名称","step":2,"category":"sop"},
    {"type":"user_input","content":"api-gateway","step":2},
    {"type":"thought","content":"用户提供了 service_name=api-gateway。按 depends_on 顺序，下一步获取 node_ip（环境注入），然后获取 db_pool_status。","step":3},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"node_ip"},"step":3,"category":"sop"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, value=192.168.1.100, source=env_injection","step":3,"category":"sop"},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"db_pool_status"},"step":4,"category":"sop"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, source=tool_call(db_pool_analyzer)\nvalue: utilization_pct=96%, waiting=12, avg_wait=8500ms, timeouts=15","step":4,"category":"sop"},
    {"type":"thought","content":"db_pool_status 显示严重饱和: utilization=96%, waiting=12, avg_wait=8500ms。需要获取 pool_saturation 让 Skill 判定级别。","step":5},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"pool_saturation"},"step":5,"category":"sop"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, source=skill_call(db-pool-saturation-detector)\nvalue: critical|slow_query_blocking|immediate\nsaturation_level=critical, inferred_cause=slow_query_blocking","step":5,"category":"sop"},
    {"type":"thought","content":"pool_saturation=critical，根因 slow_query_blocking。对照 n-1-1 前置条件:\n• pool_saturation 为 critical ✓\n• 还需确认 log_snapshot 中 connection timeout 命中次数 > 10\n先获取 log_snapshot。","step":6},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"log_snapshot"},"step":6,"category":"sop"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, source=tool_call(service_log_collector)\nmatched_lines=250, connection timeout=200, pool exhausted=50","step":6,"category":"sop"},
    {"type":"thought","content":"log_snapshot 确认 200 条 connection timeout (>10 ✓)。n-1-1 前置条件全部满足，推进到诊断节点。","step":7},
    {"type":"tool_call","name":"sop_advance","args":{"target_node_id":"n-1-1","reasoning":"pool_saturation=critical, slow_query_blocking, 200条timeout, utilization=96%"},"step":7,"category":"sop"},
    {"type":"tool_result","name":"sop_advance","content":"ok=true, 已推进到 n-1-1 (诊断节点)\nnode_type=diagnosis, is_leaf=true","step":7,"category":"sop"},
    {"type":"thought","content":"到达叶子节点 n-1-1。诊断步骤:\n[1] 调 connection_tracker → {connection_data}, {blocking_pid}\n[2] 调 service_log_collector → {log_snapshot} (已有)\n[4] 依赖[1] → slow_query_type Skill","step":8},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"connection_data"},"step":8,"category":"data"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, source=tool_call(connection_tracker)\npid=28461: SELECT * FROM orders WHERE status='pending', 22.5s, DataFileRead\npid=28503: UPDATE inventory..., 8.2s, transactionid 锁等待","step":8,"category":"data"},
    {"type":"tool_call","name":"sop_request_variable","args":{"variable_name":"slow_query_type"},"step":9,"category":"data"},
    {"type":"tool_result","name":"sop_request_variable","content":"ok=true, source=skill_call(slow-query-classifier)\nvalue: full_table_scan|P0|创建索引\naffected_tables: orders, impact=45%连接时间","step":9,"category":"data"},
    {"type":"final","content":"## 诊断结论\n\n**根因**: orders 表缺少 status 索引，SELECT * FROM orders WHERE status='pending' 全表扫描(22.5s)，耗尽连接池\n\n**快速恢复**:\n1. SELECT pg_terminate_backend(28461)\n2. 临时提升 max_connections 至 80\n\n**彻底修复**:\n1. CREATE INDEX CONCURRENTLY idx_orders_status ON orders(status)\n2. 优化大事务为分批提交\n3. 调整 autovacuum 参数","step":10},
]

# ============================================================
# Tab 1: SOP ReAct Agent Workflow
# ============================================================
tab1, tab2 = st.tabs(["🔄 SOP ReAct Agent 工作过程", "📋 测评与 GitOps 全生命周期"])

with tab1:
    st.markdown("## SOP ReAct 推理过程可视化")
    st.caption("数据来源于 [agent-resource-模版.md §3.2 Trace 示例]() — 数据库连接池耗尽排障 SOP 完整执行轨迹")

    # ----- Control -----
    col_ctrl, col_legend = st.columns([2, 1])
    with col_ctrl:
        speed = st.select_slider("播放速度", options=["慢速", "中速", "快速", "即时"], value="中速")
        delay_map = {"慢速": 1.5, "中速": 0.8, "快速": 0.3, "即时": 0.0}
        if st.button("▶ 重新播放", type="primary"):
            st.session_state.step_idx = 0

    with col_legend:
        st.markdown("""
        <small>
        🟠 SOP工具 &nbsp; 🔵 数据工具 &nbsp; 💭 思考 &nbsp; ✅ 结论<br>
        ── 依赖链 &nbsp; ⚡ 可并行
        </small>
        """, unsafe_allow_html=True)

    if "step_idx" not in st.session_state:
        st.session_state.step_idx = 0

    # ----- Progress bar -----
    max_step = max(e["step"] for e in TRACE)
    progress = st.progress(0)

    # ----- Main display area -----
    col_graph, col_trace = st.columns([1, 2])

    with col_graph:
        st.markdown("### 变量依赖 & SOP 树")

        # mini dependency graph
        deps_html = """
        <div style="font-size:13px;font-family:monospace;line-height:1.6;background:#1a1a2e;color:#e0e0e0;padding:12px;border-radius:8px">
        <b style="color:#ffd700">SOP: sop-db-pool-exhaustion</b><br>
        ├─ <span style="color:#4fc3f7">service_name</span> ← user_input<br>
        ├─ <span style="color:#4fc3f7">node_ip</span> ← env_injection<br>
        ├─ <span style="color:#4fc3f7">db_pool_status</span> ← <span style="color:#ff8a65">tool:db_pool_analyzer</span><br>
        │&nbsp;&nbsp; └─ depends: service_name, node_ip<br>
        ├─ <span style="color:#4fc3f7">pool_saturation</span> ← <span style="color:#ce93d8">skill:detector</span><br>
        │&nbsp;&nbsp; └─ depends: db_pool_status<br>
        ├─ <span style="color:#4fc3f7">connection_data</span> ← <span style="color:#ff8a65">tool:connection_tracker</span><br>
        ├─ <span style="color:#4fc3f7">slow_query_type</span> ← <span style="color:#ce93d8">skill:classifier</span><br>
        │&nbsp;&nbsp; └─ depends: connection_data<br>
        └─ <span style="color:#4fc3f7">log_snapshot</span> ← <span style="color:#ff8a65">tool:log_collector</span><br>
        <br><b style="color:#ffd700">SOP Tree:</b><br>
        n-1 根节点<br>
        ├─ <span style="color:#66bb6a">n-1-1</span> 慢查询阻塞 ← 当前命中<br>
        ├─ n-1-2 配置不足<br>
        └─ n-1-3 网络问题
        </div>
        """
        st.markdown(deps_html, unsafe_allow_html=True)

    with col_trace:
        # Show trace steps up to current index
        idx = st.session_state.step_idx
        current_step_display = min(idx, max_step)

        for i, event in enumerate(TRACE[:idx+1]):
        # for i, event in enumerate(TRACE):
            if i > idx:
                break
            etype = event["type"]

            color_map = {
                "system": "#607d8b",
                "s0_triage": "#9c27b0",
                "thought": "#ff9800",
                "tool_call": "#2196f3",
                "tool_result": "#4caf50",
                "user_input": "#00bcd4",
                "final": "#f44336",
            }
            icon_map = {
                "system": "📡",
                "s0_triage": "🎯",
                "thought": "💭",
                "tool_call": "🔧",
                "tool_result": "📊",
                "user_input": "👤",
                "final": "✅",
            }

            bg = color_map.get(etype, "#333")
            icon = icon_map.get(etype, "•")

            if etype == "tool_call":
                cat = event.get("category", "")
                cat_tag = f" [{cat.upper()}]" if cat else ""
                body = f"**{event.get('name', '')}**{cat_tag}\n`{json.dumps(event.get('args', {}), ensure_ascii=False)}`"
            elif etype == "tool_result":
                body = f"**{event.get('name', '')}**\n{event.get('content', '')}"
            elif etype == "thought":
                body = event["content"]
            elif etype == "user_input":
                body = f"👤 用户输入: **{event['content']}**"
            elif etype == "final":
                body = event["content"]
            else:
                body = event.get("content", "")

            st.markdown(f"""
            <div style="border-left:3px solid {bg};margin:4px 0;padding:6px 10px;background:#f5f5f5;border-radius:4px;font-size:13px">
            <span style="color:{bg};font-weight:bold">{icon} {etype.upper()}</span>
            <span style="color:#999;font-size:11px;float:right">Step {event['step']}</span>
            <div style="margin-top:4px;white-space:pre-wrap">{body}</div>
            </div>
            """, unsafe_allow_html=True)

        progress.progress(min(current_step_display / max_step, 1.0))

        if idx < len(TRACE) - 1:
            time.sleep(delay_map.get(speed, 0.5))
            st.session_state.step_idx = idx + 1
            st.rerun()

    # ----- Summary stats -----
    if idx >= len(TRACE) - 1:
        st.success("✅ 推理完成")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("总步数", "10")
        c2.metric("工具调用", "9 次")
        c3.metric("Skill 调用", "2 次", "detector + classifier")
        c4.metric("诊断耗时", "~54s")

# ============================================================
# Tab 2: 测评与 GitOps 全生命周期
# ============================================================
with tab2:
    st.markdown("## 测评与 GitOps 全生命周期")

    phase = st.radio("选择阶段", [
        "1️⃣ 开发阶段: 写资源 + 写测评用例",
        "2️⃣ 本地自测: 边写边跑",
        "3️⃣ PR 门禁: CI 自动测评",
        "4️⃣ 部署: ArgoCD Sync + 观察窗口",
        "5️⃣ 运维: Bad Case 回流",
    ], horizontal=True)

    if "1️⃣" in phase:
        st.markdown("### 开发阶段: 测评先行")
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("""
            **资源文件** (`agent-resources/`)
            ```
            tools/
            ├── db_pool_analyzer.yaml
            ├── service_log_collector.yaml
            └── connection_tracker.yaml
            skills/
            ├── db-pool-saturation-detector.yaml
            └── slow-query-classifier.yaml
            sops/
            └── sop-db-pool-exhaustion.md
            ```
            """)
        with col_b:
            st.markdown("""
            **测评用例** (`tests/agent-resources/`)
            ```
            tools/db_pool_analyzer.yaml          (§3.4)
              ├─ deterministic_checks ✅
              ├─ cases (4: normal/idle/not-found/unreachable)
              └─ robustness ✅
            skills/db-pool-saturation-detector.yaml (§3.5)
              ├─ trigger_cases ✅ (3: critical/warning/正常)
              ├─ core_logic_cases ✅ (6: 每条分支≥1)
              └─ robustness ✅ (3: 不完整/非法值/大数据)
            sops/sop-db-pool-exhaustion.yaml     (§3.3)
              ├─ trigger_cases ✅
              ├─ branch_coverage (3条分支各≥1) ✅
              └─ robustness ✅
            ```
            """)
        st.info("💡 **测评先行**: 定义资源的同时定义测评用例。写完一个分支就测一个分支，不要等 SOP 全部写完再补。")

    elif "2️⃣" in phase:
        st.markdown("### 本地自测: 提交前确认通过")
        st.code("""# 单资源快速测试
uv run python -m scripts.eval run \\
  --resource agent-resources/sops/sop-db-pool-exhaustion.md \\
  --test tests/agent-resources/sops/sop-db-pool-exhaustion.yaml \\
  --env docker

# 输出:
# [branch-slow-query-scan]    ✅ pass (5/5)  avg=91.7
# [branch-config-insufficient]✅ pass (5/5)  avg=88.3
# [branch-network-issue]      ⚠️  FAIL (3/5) avg=72.0
# → 修复后重跑 → 全部 pass → git push""", language="bash")
        st.warning("⚠️ `scripts.eval` 模块当前为规划中，尚未实现。目前替代方案: 人工按 Trace 逐条对比。")

    elif "3️⃣" in phase:
        st.markdown("### PR 门禁: CI 自动拦截")
        pipeline = """
        ```mermaid
        graph TD
            PR[PR 提交] --> J0{Job 0: 测评用例存在?}
            J0 -->|缺失| BLOCK[❌ 阻断: 请补充测评用例]
            J0 -->|存在| J1[Job 1: 格式校验]
            J1 --> J2[Job 2: 测评执行<br/>Docker Compose 隔离环境]
            J2 --> J3{Job 3: 门禁判定}
            J3 -->|退化 > 10%| BLOCK2[❌ 阻断: 评论退化详情]
            J3 -->|新增 ≥ 80%| PASS[✅ 通过]
            J3 -->|修改 ≥ 95%| PASS
            PASS --> MERGE[合并到 main]
        ```
        """
        st.markdown(pipeline, unsafe_allow_html=True)
        st.info("**Job 0 是硬门禁**: 新增资源没有配套测评用例 → 5 秒内直接阻断，不需要跑后续的 30 分钟测评。")

    elif "4️⃣" in phase:
        st.markdown("### 部署: ArgoCD + 观察窗口")
        deploy_cols = st.columns(3)
        with deploy_cols[0]:
            st.markdown("**PreSync Hook**")
            st.markdown("""
            1. 解析 agent-resources/ 所有文件
            2. 校验 tool_call/skill_call 引用的资源存在且启用
            3. 校验失败 → Sync Failed → **阻断部署**
            4. 校验通过 → API 写入数据库
            """)
        with deploy_cols[1]:
            st.markdown("**Agent 更新**")
            st.markdown("""
            1. conversation-service 热加载新 Tool/Skill/SOP
            2. Agent Pod 无需重启
            3. 下一次 ReAct 调用自动使用新定义
            """)
        with deploy_cols[2]:
            st.markdown("**30min 观察窗口**")
            st.markdown("""
            | 时间 | 检查项 |
            |------|--------|
            | 1min | Pod Running |
            | 5min | 变量成功率 > 95% |
            | 10min | 命令延迟 < 基线2x |
            | 30min | SOP 命中率无退化 |
            """)

    elif "5️⃣" in phase:
        st.markdown("### 运维: Bad Case 回流闭环")
        st.markdown("""
        ```mermaid
        graph LR
            PROD[线上低分会话] --> EXTRACT[每日自动提取<br/>8 种信号源]
            EXTRACT --> CLASSIFY[自动分类<br/>幻觉/路由/工具/变量/效率]
            CLASSIFY --> GEN[生成待确认用例<br/>tests/badcases/]
            GEN --> REVIEW[人工确认 expected]
            REVIEW --> PROMOTE[移入正式目录<br/>纳入回归套件]
            PROMOTE --> CI[CI 永久覆盖]
        ```
        """)
        bc_cols = st.columns(4)
        bc_cols[0].metric("信号源", "8 种", "用户评分/未解决/异常终止/工具失败/重复提问/幻觉/升级/SOP未命中")
        bc_cols[1].metric("提取频率", "每日 1 次", "凌晨自动执行")
        bc_cols[2].metric("人工确认", "每周 1 次", "批量审核待确认用例")
        bc_cols[3].metric("回流周期", "1-2 周", "从 Bad Case 到 CI 覆盖")

st.markdown("---")
st.caption("数据来源: [agent-resource-模版.md](01-模版与规范/agent-resource-模版.md) §3.2 | [agent-测评与GitOps方案.md](01-模版与规范/agent-测评与GitOps方案.md)")
