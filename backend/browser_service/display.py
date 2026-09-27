"""A disposable Linux desktop; only its VNC listener is exposed during verification."""
import asyncio
import os
import socket
import sys
from pathlib import Path


class PrivateDisplay:
    def __init__(self):
        self.processes = []
        self.vnc = None
        self.number = None
        self.port = None

    async def start(self):
        if os.name == 'nt' or sys.platform == 'darwin':
            return {}
        # Exclusive X lock and service concurrency prevent two verification desktops sharing.
        for number in range(90, 120):
            if not Path(f'/tmp/.X{number}-lock').exists():
                self.number = number
                break
        if self.number is None:
            raise RuntimeError('没有可用的验证显示环境')
        display = f':{self.number}'
        proc = await asyncio.create_subprocess_exec('Xvfb', display, '-screen', '0',
            '1280x900x24', '-nolisten', 'tcp', '-noreset',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.processes.append(proc)
        for _ in range(50):
            if proc.returncode is not None:
                raise RuntimeError('验证显示环境启动失败')
            if Path(f'/tmp/.X11-unix/X{self.number}').exists():
                break
            await asyncio.sleep(.1)
        else:
            raise RuntimeError('验证显示环境启动超时')
        return {**os.environ, 'DISPLAY': display}

    async def expose(self):
        if os.name == 'nt' or sys.platform == 'darwin':
            return
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.vnc = await asyncio.create_subprocess_exec('x0vncserver',
            '-display', f':{self.number}', '-rfbport', str(self.port),
            '-localhost', 'yes', '-SecurityTypes', 'None',
            '-AlwaysShared', 'yes', '-AcceptCutText', '0', '-SendCutText', '0',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(50):
            if self.vnc.returncode is not None:
                raise RuntimeError('远程验证连接启动失败')
            try:
                _, writer = await asyncio.open_connection('127.0.0.1', self.port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(.1)
        raise RuntimeError('远程验证连接启动超时')

    async def hide(self):
        if self.vnc:
            await self._stop(self.vnc)
            self.vnc = None
        self.port = None

    async def close(self):
        await self.hide()
        for proc in reversed(self.processes):
            await self._stop(proc)
        self.processes.clear()

    @staticmethod
    async def _stop(proc):
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
