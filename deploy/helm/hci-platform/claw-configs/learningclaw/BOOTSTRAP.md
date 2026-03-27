# LearningClaw 启动引导 · BOOTSTRAP

> 每次 session 启动时，执行以下步骤完成初始化和自检。

---

## 第一步：环境自检

```
检查以下环境变量是否存在：
✅ KB_SERVICE_URL      - 知识库服务地址
✅ CASE_SERVICE_URL    - 工单服务地址
✅ CONVERSATION_SERVICE_URL - 对话服务地址
✅ INTERNAL_API_TOKEN  - 内部 API 鉴权 Token
✅ LEARNINGCLAW_MODE   - 运行模式（batch/event/manual）

如果缺少任何必要变量，记录到日志并等待，不要执行任何写入操作。
```

---

## 第二步：读取身份和灵魂文件

按顺序阅读以下文件，建立本次 session 的自我认知：
1. `/home/node/.openclaw/workspace/SOUL.md` — 我是谁，我的价值观
2. `/home/node/.openclaw/workspace/IDENTITY.md` — 我的角色和边界
3. `/home/node/.openclaw/workspace/AGENTS.md` — 我的工作规范
4. `/home/node/.openclaw/workspace/TOOLS.md` — 我有什么工具
5. `/home/node/.openclaw/workspace/USER.md` — 我在什么环境中工作

---

## 第三步：读取学习进度

读取 Memory 文件，了解：
- 上次批处理的最后一个案例 ID（`last_processed_case_id`）
- 上次处理的日期
- 待处理队列中的工单 ID 列表

```
文件路径：/home/node/.openclaw/workspace/memory/learning_progress.md
如果文件不存在，这是首次启动，从第一篇案例开始学习。
```

---

## 第四步：根据运行模式选择任务

### 模式 A：`batch`（定时批处理）
```
目标：从 Sangfor 案例库学习新案例
来源：https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id=36402

1. 从 last_processed_case_id 的下一页开始
2. 每批处理 20 篇案例
3. 处理完成后更新 learning_progress.md
4. 输出今日学习报告到 memory/YYYY-MM-DD.md
```

### 模式 B：`event`（工单关闭触发）
```
环境变量 TRIGGER_CASE_ID 包含刚关闭的工单 ID

1. 读取该工单的完整对话记录
2. 判断是否产生了新知识（与现有库对比）
3. 如有新知识，摄入知识库
4. 更新 learning_progress.md
```

### 模式 C：`manual`（手动触发）
```
环境变量 MANUAL_TASK 描述了具体任务

按任务说明执行，完成后输出完整报告。
```

---

## 第五步：执行完成后的收尾

1. 将本次执行摘要追加到 `memory/YYYY-MM-DD.md`
2. 更新 `memory/learning_progress.md`（进度文件）
3. 调用 `GET {KB_SERVICE_URL}/api/kb/stats` 确认知识库数据增长
4. 打印本次学习结果：
   - 处理案例数
   - 成功摄入知识条目数
   - 跳过/失败数
   - 当前知识库总量

---

## 异常处理

如果在执行过程中遇到无法处理的错误：
1. 记录详细错误信息到 memory
2. **保持 session 运行，不要退出**（等待下次调度）
3. 不要用未完成的内容覆盖 learning_progress.md

---

> 启动完成标志：打印 "✅ LearningClaw 已就绪，开始执行 [MODE] 任务"
