"""
学校官网信息订阅 — 入口
===============================
跨平台 Web 应用，用于聚合多所学校官网通知，
按官网栏目订阅、阅读与管理信息，支持移动端访问。
"""

import logging
import os

from dotenv import load_dotenv

# 加载 .env 文件（必须在导入 backend / create_app 之前）
load_dotenv()

from backend import create_app  # noqa: E402

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

app = create_app()


if __name__ == '__main__':
    from waitress import serve
    host = os.environ.get('WATCHER_HOST', '127.0.0.1')
    port = int(os.environ.get('WATCHER_PORT', '5000'))
    print(f'School Watcher: http://localhost:{port}', flush=True)
    print('Choose a school in the directory to subscribe. Press Ctrl+C to stop.', flush=True)
    serve(app, host=host, port=port, threads=max(2, min(64, int(os.environ.get('WATCHER_WEB_THREADS', '4')))))
