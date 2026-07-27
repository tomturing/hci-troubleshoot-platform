# Shell / Makefile / CI 脚本避坑

## PIT-001：here-doc 在 shell 函数内失效

`<<'EOF'` 在某些 shell 函数上下文中需要注意缩进，使用 `<<-'EOF'` 允许 tab 缩进。

## PIT-002：nohup 后台命令的输出重定向

```bash
# 错误：输出混入终端
nohup long-cmd &

# 正确：明确重定向
nohup long-cmd > /tmp/cmd.log 2>&1 &
echo "PID=$!"
```

## D-012：GitHub Actions 无路径过滤时，局部变更 PR 触发全套 CI（>10min）

**现象**：只改了 2 个 Helm YAML 文件的 PR（如 PR #506），CI 运行超过 10 分钟，全套 Job 全部触发。

**根因（5 个叠加）**：
1. `ci.yml` 所有 Job 无 `paths` 过滤，每次 PR 全量触发
2. `uv sync --group dev` 在 4 个 Job 各跑一次，`astral-sh/setup-uv` 未开启 `enable-cache`，无缓存每次 ~3min
3. `pnpm install` 在 2 个 Job 各跑一次，无 pnpm store cache，无缓存每次 ~5min
4. `unit-tests` 的 `needs` 错误包含 `frontend-build`，Python 单测被迫等待前端构建完成（Critical Path 浪费 ~10min）
5. `helm-validate` 每次从 GitHub Release 重新下载 helm-unittest 插件

**三档修复方案（详见 `docs/deploy/events/2026-07-09-CI检查超时分析与优化方案.md`）**：

```yaml
# 档位 A（立即可用）：开启缓存
# 1. uv 缓存
- uses: astral-sh/setup-uv@v3
  with:
    enable-cache: true
    cache-dependency-glob: "uv.lock"

# 2. pnpm store cache（frontend Job 中 install 前插入）
- uses: actions/cache@v4
  with:
    path: ~/.local/share/pnpm/store
    key: ${{ runner.os }}-pnpm-${{ hashFiles('frontend/pnpm-lock.yaml') }}

# 3. helm-unittest 插件 cache（helm-validate Job 中 curl 前插入）
- uses: actions/cache@v4
  with:
    path: ~/.local/share/helm/plugins
    key: helm-unittest-v0.4.4
```

```yaml
# 档位 B（中期）：去掉 unit-tests 对 frontend 的无效依赖
unit-tests:
  needs: [lint, helm-validate, probe-alignment]  # 删除 frontend-build, frontend-unit-test
```

```yaml
# 档位 C（彻底解决）：dorny/paths-filter@v3 路径过滤
# push main 时仍强制全跑；仅 PR 事件生效路径过滤
lint:
  needs: [changes]
  if: github.event_name != 'pull_request' || needs.changes.outputs.backend == 'true'
helm-validate:
  needs: [changes]
  if: github.event_name != 'pull_request' || needs.changes.outputs.helm == 'true'
```

## D-016：`pipefail` 与 `grep -q` 组合导致生产者 SIGPIPE 假失败

**触发场景**：脚本启用 `set -euo pipefail`，再用 `producer | grep -q pattern` 验证长列表中是否存在目标。典型生产者包括 `k3s ctr images ls`、`kubectl get ...` 和输出量较大的 CLI。

**症状**：目标明明存在，`grep -q` 单独判断也能匹配，但 `if producer | grep -q ...; then` 却进入失败分支；本次实例表现为 containerd 导入命令成功后，脚本误报“未找到该镜像”。

**根因**：`grep -q` 找到首个匹配后会立即退出并关闭管道。生产者继续写入时收到 SIGPIPE，返回非零；`pipefail` 让整个 pipeline 采用该非零状态，因此条件被判为失败。列表越长，越容易复现。

**正确做法**：先完整读取生产者结果，再对已读取的字符串匹配，使生产者不会被提前关闭：

```bash
image_refs="$(k3s ctr images ls -q)"
if grep -Eq '(^|/)hci-api-gateway:dev-local$' <<< "$image_refs"; then
  echo "镜像存在"
fi
```

也可以使用会消费完整输入且显式管理退出码的 `awk`，但不要仅通过去掉 `pipefail` 隐藏其他真实失败。

**预防检查**：审查启用 `pipefail` 的 Shell 时，搜索 `| grep -q`；如果左侧可能输出多行或持续流式输出，改成“先捕获、后匹配”。2026-07-27 dev K3s 镜像导入验收已据此修复 `scripts/ops/k3s-build.sh`。
