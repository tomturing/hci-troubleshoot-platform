# terminal_bridge

HCI 排障助手本地 SSH Bridge 组件。

## 架构

```
Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
```

- 公网服务器不参与任何 SSH 流量
- HCI 设备无需改动，只需开放 SSH 22 端口
- Windows 端双击运行，托盘图标，客户无感知

## 打包（Windows 上执行）

```bat
cd terminal_bridge
build_windows.bat
```

打包完成后将 `dist\terminal_bridge.exe` 复制到发布目录：

```
frontend\customer\public\downloads\terminal_bridge.exe
```

然后执行发布：

```bash
bash scripts/k3s-release.sh --services customerUI
```

## WebSocket 消息协议

### 浏览器 → Bridge

| type | 字段 | 说明 |
|------|------|------|
| `ssh_connect` | `host, username, port, auth_type, password?, private_key?, case_id` | 发起连接 |
| `ssh_input` | `data, case_id` | 键盘输入（含回车） |
| `ssh_inject_command` | `command, case_id` | AI 助手注入命令（不带 \\n） |
| `ssh_disconnect` | `case_id` | 断开连接 |

### Bridge → 浏览器

| type | 字段 | 说明 |
|------|------|------|
| `ssh_connected` | `case_id` | 连接成功 |
| `ssh_output` | `case_id, output` | SSH 输出流 |
| `ssh_error` | `case_id, message` | 连接失败 |
| `ssh_disconnected` | `case_id` | 已断开 |

## 开发测试

```bash
# Linux/Mac 上可以直接运行 Python 源码进行调试
pip install websockets pystray pillow
python terminal_bridge/main.py
```
