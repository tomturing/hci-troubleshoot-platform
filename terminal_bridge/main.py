#!/usr/bin/env python3
"""
terminal_bridge.py
==================
Windows 本地 SSH Bridge 组件

架构：
  Custom UI (浏览器) → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI Linux

公网服务器不参与任何 SSH 流量。
"""

import asyncio
import json
import subprocess
import threading
import sys
import os
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[ERROR] 请先安装依赖: pip install websockets pystray pillow")
    sys.exit(1)

WS_HOST = "localhost"
WS_PORT = 9999

# ─── SSH 会话管理 ────────────────────────────────────────────────────────

class SSHSession:
    def __init__(self, case_id: str, host: str, user: str, port: int = 22,
                 password: str = None, private_key: str = None):
        self.case_id = case_id
        self.host = host
        self.user = user
        self.port = port
        self.password = password
        self.private_key_content = private_key
        self.process = None
        self._output_callbacks = []
        self._lock = threading.Lock()

    def start(self):
        """\u542f动 SSH 进程（使用 Windows 系统自带 ssh.exe）"""
        cmd = [
            "ssh", "-tt",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=no",
            "-p", str(self.port),
        ]

        # 密钥认证：将私钥内容写入临时文件
        self._key_file = None
        if self.private_key_content:
            import tempfile
            self._key_file = tempfile.NamedTemporaryFile(
                mode='w', suffix='.pem', delete=False
            )
            self._key_file.write(self.private_key_content)
            self._key_file.flush()
            self._key_file.close()
            cmd += ["-i", self._key_file.name, "-o", "PasswordAuthentication=no"]

        cmd.append(f"{self.user}@{self.host}")

        # Windows: 不弹黑色框
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW

        # 密码认证时设置环境变量（sshpass 不可用，用 SSH_ASKPASS 方案）
        env = os.environ.copy()
        if self.password:
            # Windows 下用 STDIN 按需发送密码
            pass

        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creation_flags,
            env=env,
        )

        # 密码认证：当 SSH 要求密码时发送
        if self.password:
            threading.Thread(
                target=self._send_password_when_prompted,
                daemon=True
            ).start()

        # 启动输出读取线程
        threading.Thread(target=self._read_output, daemon=True).start()

    def _send_password_when_prompted(self):
        """\u7b49待 SSH 要求密码后自动输入"""
        import time
        time.sleep(1.5)  # 等待密码提示出现
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(f"{self.password}\n".encode())
                self.process.stdin.flush()
            except Exception:
                pass

    def _read_output(self):
        """\u6301续读取 SSH 输出并回调"""
        while self.process and self.process.poll() is None:
            try:
                chunk = self.process.stdout.read(1024)
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    with self._lock:
                        callbacks = list(self._output_callbacks)
                    for cb in callbacks:
                        cb(text)
            except Exception:
                break

    def send_input(self, data: str):
        """\u53d1送键盘输入（包括回车）"""
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write(data.encode())
                self.process.stdin.flush()
            except Exception as e:
                print(f"[Bridge] send_input error: {e}")

    def inject_command(self, command: str):
        """AI 助手注入命令（不带 \\n，等客户回车确认）"""
        self.send_input(command)  # 不带 \n

    def on_output(self, callback):
        with self._lock:
            self._output_callbacks.append(callback)

    def close(self):
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        # 清理临时私钥文件
        if hasattr(self, '_key_file') and self._key_file:
            try:
                os.unlink(self._key_file.name)
            except Exception:
                pass


# ─── WebSocket Server ────────────────────────────────────────────────────────

# case_id → SSHSession
sessions: dict = {}

async def handle_client(websocket):
    print("[Bridge] 浏览器已连接")
    loop = asyncio.get_event_loop()

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            case_id = msg.get("case_id", "default")

            if msg_type == "ssh_connect":
                # 如果已有连接则先关闭
                old = sessions.pop(case_id, None)
                if old:
                    old.close()

                session = SSHSession(
                    case_id=case_id,
                    host=msg["host"],
                    user=msg["username"],
                    port=int(msg.get("port", 22)),
                    password=msg.get("password"),
                    private_key=msg.get("private_key"),
                )

                def make_output_cb(cid):
                    def cb(text):
                        payload = json.dumps({
                            "type": "ssh_output",
                            "case_id": cid,
                            "output": text
                        })
                        asyncio.run_coroutine_threadsafe(
                            websocket.send(payload), loop
                        )
                    return cb

                session.on_output(make_output_cb(case_id))

                try:
                    session.start()
                    sessions[case_id] = session
                    await websocket.send(json.dumps({
                        "type": "ssh_connected",
                        "case_id": case_id
                    }))
                    print(f"[Bridge] SSH 已连接: {msg['username']}@{msg['host']}")
                except Exception as e:
                    await websocket.send(json.dumps({
                        "type": "ssh_error",
                        "case_id": case_id,
                        "message": str(e)
                    }))

            elif msg_type == "ssh_inject_command":
                session = sessions.get(case_id)
                if session:
                    # 不带 \n，填入命令行等客户回车
                    session.inject_command(msg.get("command", ""))

            elif msg_type == "ssh_input":
                session = sessions.get(case_id)
                if session:
                    # 键盘输入，直接透传（包括 \n）
                    session.send_input(msg.get("data", ""))

            elif msg_type == "ssh_disconnect":
                session = sessions.pop(case_id, None)
                if session:
                    session.close()
                await websocket.send(json.dumps({
                    "type": "ssh_disconnected",
                    "case_id": case_id
                }))
                print(f"[Bridge] SSH 已断开: case={case_id}")

    except websockets.exceptions.ConnectionClosed:
        print("[Bridge] 浏览器已断开")
    except Exception as e:
        print(f"[Bridge] 处理异常: {e}")


async def start_ws_server():
    print(f"[Bridge] WebSocket 监听中: ws://{WS_HOST}:{WS_PORT}")
    async with websockets.serve(handle_client, WS_HOST, WS_PORT):
        await asyncio.Future()  # 永久运行


# ─── 系统托盘 (仅 Windows) ─────────────────────────────────────────────────

def run_tray():
    try:
        import pystray
        from PIL import Image, ImageDraw

        # 生成一个简单的图标
        img = Image.new("RGB", (64, 64), color=(30, 120, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([16, 20, 48, 44], fill=(255, 255, 255))

        def on_quit(icon, item):
            icon.stop()
            os._exit(0)

        icon = pystray.Icon(
            "HCI Bridge",
            img,
            "HCI 排障助手 - Bridge",
            menu=pystray.Menu(
                pystray.MenuItem(
                    f"ws://localhost:{WS_PORT}",
                    lambda *_: None,
                    enabled=False
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", on_quit),
            )
        )
        icon.run()
    except Exception as e:
        print(f"[Bridge] 托盘初始化失败 ({e})，无图标模式运行")
        # 没有托盘也能正常运行


# ─── 入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Windows 下在独立线程运行托盘图标
    if sys.platform == "win32":
        tray_thread = threading.Thread(target=run_tray, daemon=True)
        tray_thread.start()
    else:
        print("[Bridge] 非 Windows 环境，跳过托盘初始化")

    print("[Bridge] HCI SSH Bridge 已启动")
    print(f"[Bridge] 监听地址: ws://{WS_HOST}:{WS_PORT}")
    print("[Bridge] 右键托盘图标可退出")

    asyncio.run(start_ws_server())
