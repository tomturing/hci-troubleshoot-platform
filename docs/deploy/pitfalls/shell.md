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
