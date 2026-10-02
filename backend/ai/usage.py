"""Read-only usage aggregates from execution records, never from duplicated budget rows."""
from datetime import datetime, timedelta

from backend.ai.models import AIExecution, AIBudget
from backend.ai.providers import PROVIDER_NAMES
from backend.database.db import db
from backend.database.models import AppConfig


def _counter():
    return dict(tokens=0, input_tokens=0, output_tokens=0, unclassified_tokens=0,
                calls=0, succeeded=0, failed=0, uncertain=0, pending=0, unknown_usage=0)


def _integer(value):
    return value if type(value) is int and value >= 0 else 0


def usage_dashboard(days=30, offset_minutes=0, *, now=None):
    """Days use the viewer's UTC offset. Monthly quotas retain the runtime's UTC clock."""
    days, offset_minutes = int(days), int(offset_minutes)
    if days not in (7, 30, 90) or not -840 <= offset_minutes <= 840:
        raise ValueError('请选择有效的统计时段')
    now = now or datetime.utcnow()
    offset = timedelta(minutes=offset_minutes)
    today = (now + offset).date()
    first = today - timedelta(days=364)
    selected_first = today - timedelta(days=days - 1)
    calendar = {first + timedelta(days=i): _counter() for i in range(365)}
    total, lifetime, models, purposes = _counter(), _counter(), {}, {}
    history_start = None
    # Only select metadata needed by the charts; prompts, outputs and keys never leave the DB.
    rows = db.session.query(AIExecution.created_at, AIExecution.provider, AIExecution.model,
                            AIExecution.purpose, AIExecution.skill_id, AIExecution.status,
                            AIExecution.usage).filter(
        AIExecution.created_at <= now).yield_per(1000)
    for row in rows:
        # Reserved requests may not have been dispatched yet.
        if row.status == 'reserved':
            continue
        date = (row.created_at + offset).date()
        history_start = min(history_start, date) if history_start else date
        usage = row.usage if isinstance(row.usage, dict) else {}
        known = usage.get('known') is True and type(usage.get('total_tokens')) is int and usage['total_tokens'] >= 0
        tokens = usage['total_tokens'] if known else 0
        inp, out = _integer(usage.get('input_tokens')), _integer(usage.get('output_tokens'))
        # Reconciliation can supply a total without an input/output breakdown.
        if not known or inp + out > tokens:
            inp = out = 0
        counts = _counter()
        counts.update(tokens=tokens, input_tokens=inp, output_tokens=out,
                      unclassified_tokens=tokens - inp - out, calls=1,
                      unknown_usage=int(not known and row.status != 'sending'))
        status_key = {'sending': 'pending', 'succeeded': 'succeeded',
                      'failed': 'failed', 'uncertain': 'uncertain'}.get(row.status, 'uncertain')
        counts[status_key] = 1
        targets = [lifetime]
        if date in calendar:
            targets.append(calendar[date])
        if date >= selected_first:
            # Connection tests share the summary budget, but get their own usage category.
            purpose = 'test' if row.skill_id == 'connection-test' else row.purpose
            if purpose not in ('directory', 'summary', 'test'):
                purpose = 'other'
            model_key = (row.provider, row.model)
            targets += [total, models.setdefault(model_key, _counter()), purposes.setdefault(purpose, _counter())]
        for target in targets:
            for key, value in counts.items():
                target[key] += value
    daily = [dict(date=day.isoformat(), **counts) for day, counts in calendar.items()]
    series = daily[-days:]
    completed = total['succeeded'] + total['failed']
    total['success_rate'] = round(total['succeeded'] / completed * 100, 1) if completed else None
    peak = max(series, key=lambda item: item['tokens'])
    total.update(peak_tokens=peak['tokens'], peak_date=peak['date'] if peak['tokens'] else None)
    month = now.strftime('%Y-%m')
    budgets = []
    for key, label in (('total', '全部调用'), ('directory', '栏目发现'), ('summary', '通知摘要与连接测试')):
        row = db.session.get(AIBudget, key + ':' + month)
        budgets.append(dict(key=key, label=label,
                            used_tokens=max(0, row.used_tokens) if row else 0,
                            reserved_tokens=max(0, row.reserved_tokens) if row else 0,
                            limit=_integer(int(AppConfig.get('ai_token_limit_' + key, '0') or 0))))
    labels = {'directory': '栏目发现', 'summary': '通知摘要', 'test': '连接测试', 'other': '其他调用'}
    return dict(days=days, start=selected_first.isoformat(), end=today.isoformat(),
                offset_minutes=offset_minutes, generated_at=now.isoformat() + 'Z',
                summary=total, daily=series, calendar=daily,
                lifetime=dict(start=history_start.isoformat() if history_start else None,
                              end=today.isoformat(), **lifetime),
                models=sorted([dict(provider=provider, provider_name=PROVIDER_NAMES.get(provider, provider),
                                    model=model, **counts) for (provider, model), counts in models.items()],
                              key=lambda item: (-item['tokens'], -item['calls'], item['model'])),
                purposes=[dict(key=key, label=labels[key], **purposes[key]) for key in labels if key in purposes],
                budget_month=month, budgets=budgets)
