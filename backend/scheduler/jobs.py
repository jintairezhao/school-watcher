"""定时任务调度

使用 APScheduler 实现后台定时爬取和摘要生成。
"""

import logging
from apscheduler.schedulers.background import BackgroundScheduler

from backend.database.models import AppConfig

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# 持有 Flask 应用实例，供后台定时任务推入应用上下文。
# APScheduler 的 BackgroundScheduler 在线程池中执行任务，不会自动携带
# Flask 应用上下文，因此这里必须显式保存 app 并在任务内 app.app_context() 包裹。
_app = None


def _get_interval_minutes() -> int:
    """从配置中获取爬取间隔（分钟），默认30分钟"""
    val = AppConfig.get('scrape_interval', '30')
    try:
        return max(5, int(val))
    except ValueError:
        return 30


def _scrape_job():
    """定时爬取任务"""
    from backend.scraper.engine import scrape_all_schools
    from backend.ai.summarizer import batch_summarize

    global _app
    if _app is None:
        logger.error("定时爬取跳过：Flask 应用上下文未注入（_app 为空）")
        return

    logger.info("=== 定时爬取开始 ===")
    # 后台线程无应用上下文，必须手动推入，否则 School.query 会抛
    # "Working outside of application context"。
    with _app.app_context():
        try:
            results = scrape_all_schools()
            total_new = sum(r.new_count for r in results)
            logger.info(f"定时爬取完成: 共处理 {len(results)} 所学校，新增 {total_new} 条通知")
            if total_new > 0:
                logger.info("开始为新通知生成摘要...")
                batch_summarize()
        except Exception as e:
            logger.error(f"定时爬取出错: {e}")
    logger.info("=== 定时爬取结束 ===")


def _health_check_job():
    """选择器健康检查（轮转抽样）：不依赖抓取流程，主动发现静默劣化。

    每天跑一次，按 id 轮转抽 8 个部门：抓取列表页 → 监督器评估
    （结构坏→自动修复；抓错→标人工复核）。WAF 壳页由监督器识别。
    """
    from backend.database.db import db
    from backend.database.models import Department
    from backend.scraper.engine import _fetch_html
    from backend.scraper.selector_monitor import evaluate_and_repair
    from datetime import date

    global _app
    if _app is None:
        return
    with _app.app_context():
        try:
            day = date.today().toordinal()
            # 只体检活跃学校（上架且有订阅）的部门
            from backend.database.models import School
            depts = (Department.query.join(School, Department.school_id == School.id)
                     .filter(School.enabled.is_(True), School.subscriber_count > 0)
                     .order_by(Department.id).all())
            if not depts:
                return
            sample = [d for i, d in enumerate(depts) if (i + day) % max(1, len(depts) // 8) == 0][:8]
            for dept in sample:
                if not (dept.list_url or '').strip():
                    continue
                try:
                    html = _fetch_html(dept.list_url)
                except Exception:
                    continue
                if not html:
                    continue
                outcome = evaluate_and_repair(dept, html)
                if outcome.get('action') not in (None, 'skip', 'healthy'):
                    logger.info(f"[健康检查] {dept.name}: {outcome}")
        except Exception as e:
            logger.error(f"选择器健康检查出错: {e}")


def start_scheduler(app=None):
    """启动定时任务调度器"""
    global _app
    if app is not None:
        _app = app
    interval = _get_interval_minutes()
    scheduler.add_job(
        _scrape_job,
        'interval',
        minutes=interval,
        id='scrape_job',
        name='定时爬取学校通知',
        replace_existing=True,
    )
    scheduler.add_job(
        _health_check_job,
        'interval',
        hours=24,
        id='health_check_job',
        name='选择器健康检查（轮转抽样）',
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"定时调度已启动，间隔: {interval} 分钟")


def restart_scheduler():
    """重启调度器（用于间隔变更后）"""
    interval = _get_interval_minutes()
    if scheduler.get_job('scrape_job'):
        scheduler.reschedule_job(
            'scrape_job',
            trigger='interval',
            minutes=interval,
        )
        logger.info(f"调度间隔已更新为: {interval} 分钟")


def stop_scheduler():
    """停止调度器"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("定时调度已停止")
