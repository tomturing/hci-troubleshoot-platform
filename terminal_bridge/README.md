# terminal_bridge（Go 版）

HCI 排障助手本地 SSH Bridge，Go 原生编译，无运行时依赖。

## 对比 Python 版

| | Python (PyInstaller) | Go |
|---|---|---|
| 体积 | ~16MB | ~3-4MB（upx 后约 1.5MB）|
| Win7 支持 | ❌ | ✅ |
| Win10/11 | ✅ | ✅ |
| 运行时依赖 | Python 运行时打包 | 无 |
| 启动速度 | 慢（解压）| 快 |

## 编译（在 Windows 上执行）

### 前置条件

安装 Go 1.21+：https://go.dev/dl/ → 选 `go1.21.x.windows-amd64.msi`

### 一键编译

```bat
cd terminal_bridge
build_windows.bat
```

产物 `terminal_bridge.exe` 复制到：

```
frontend\customer\public\downloads\terminal_bridge.exe
```

然后执行发布：

```bash
bash scripts/k3s-release.sh --services customerUI
```

## 架构

```
Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux
```

公网服务器不参与任何 SSH 流量，HCI 设备无需改动。

## CORS 支持（Private Network Access）

Chrome 104+ 引入了 [Private Network Access](https://developer.chrome.com/blog/private-network-access-preflight/) 安全机制：

- 从**公网域名**（如 `acli.sangfor.com.cn`）访问 `localhost` 时
- 浏览器会阻止 WebSocket 连接，除非服务端返回特定的 CORS 头

terminal_bridge 已内置 CORS 支持，握手响应包含：

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Private-Network: true
```

这使得用户可以从任意域名访问本地 bridge，无需额外配置。

## WebSocket 消息协议

### 浏览器 → Bridge

| type | 字段 | 说明 |
|---|---|---|
| `ssh_connect` | `host, username, port, auth_type, password?, private_key?, case_id` | 发起连接 |
| `ssh_input` | `data, case_id` | 键盘输入（含回车）|
| `ssh_inject_command` | `command, case_id` | AI 助手注入命令（不带 \\n，等客户回车确认）|
| `ssh_disconnect` | `case_id` | 断开连接 |

### Bridge → 浏览器

| type | 字段 | 说明 |
|---|---|---|
| `ssh_connected` | `case_id` | SSH 认证成功并进入终端会话 |
| `ssh_output` | `case_id, output` | SSH 输出流 |
| `ssh_error` | `case_id, message, detail?` | 连接失败；`message` 为标准中文提示，`detail` 为原始 SSH 关键输出 |
| `ssh_disconnected` | `case_id` | 已断开 |
