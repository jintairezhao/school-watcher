"""
学校通知扒取工具 — 入口
===============================
跨平台 Web 应用，用于聚合多所学校官网通知，
接入 DeepSeek API 生成摘要，支持移动端访问。
"""

import logging

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
    print("""
+==================================================+
|       School Notification Watcher v1.0            |
|                                                  |
|  Local:  http://localhost:5000                   |
|  Mobile: Use ngrok or other tunnel tool          |
|                                                  |
|  First time setup:                               |
|  1. Open http://localhost:5000/settings          |
|  2. Configure DeepSeek API Key                   |
|  3. Add target schools                           |
+==================================================+
    """)

    # 检查 Playwright 可用性（用于 JS 渲染页面回退）
    try:
        from backend.scraper.fetchers.playwright_fetcher import is_playwright_available
        if is_playwright_available():
            print("[OK] Playwright Chromium — JS 渲染回退已就绪")
        else:
            print("[INFO] Playwright 未安装 — JS 页面将使用纯静态模式")
    except Exception:
        print("[INFO] Playwright 未安装 — JS 页面将使用纯静态模式")

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
