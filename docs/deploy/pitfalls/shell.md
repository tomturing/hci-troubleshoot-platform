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
