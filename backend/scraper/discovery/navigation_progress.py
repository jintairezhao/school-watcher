"""Durable decision clocks for observed forward discovery branches."""
import hashlib
import json

from filelock import FileLock

from backend.services.discovery_policy import advance_progress, progress_state
from backend.services.source_inventory import canonical_url


def branches(inventory, site_key):
    """Use current forward edges; chrome cannot lead back through the school.

    A real unit or directory starts its own clock. School-level entrances are
    independent too. Multiple distinct forward parents are ambiguous, so they
    never merge two otherwise separate clocks. This is scheduling evidence,
    not a claim about administrative or publishing ownership.
    """
    with inventory.connect() as connection:
        pages = {canonical_url(row['url']): dict(row) for row in connection.execute(
            'SELECT * FROM pages WHERE site_key=?', (site_key,))}
        edges = connection.execute('SELECT parent_url,target_url,decision,content_hash FROM edges '
                                   'WHERE site_key=?', (site_key,)).fetchall()
    parents = {}
    for edge in edges:
        parent_url, target_url = map(canonical_url, (edge['parent_url'], edge['target_url']))
        parent, target = pages.get(parent_url), pages.get(target_url)
        if (edge['decision'] not in ('follow', 'official_external_link') or not parent or not target
                or parent['state'] != 'fetched' or not parent['content_hash']
                or parent['content_hash'] != edge['content_hash'] or parent['depth'] >= target['depth']):
            continue
        parents.setdefault(target_url, set()).add(parent_url)
    assignments = {}
    for url, page in sorted(pages.items(), key=lambda pair: (pair[1]['depth'], pair[0])):
        ancestors = parents.get(url, set())
        options = {assignments[parent] for parent in ancestors if parent in assignments}
        own = (page['kind'] in ('root', 'unit', 'directory') or not options or len(options) != 1
               or any(pages[parent]['kind'] == 'root' for parent in ancestors))
        assignments[url] = url if own else next(iter(options))
    return pages, assignments


def branch_url(inventory, site_key, page_url):
    _, assignments = branches(inventory, site_key)
    url = canonical_url(page_url)
    return assignments.get(url, url)


def progress_ids(inventory, site_key, school_id, page_url):
    """Only actual material and installed records in this branch are progress."""
    from backend.database.models import Announcement, Department
    pages, assignments = branches(inventory, site_key)
    url = canonical_url(page_url)
    branch = assignments.get(url, url)
    members = {address for address, value in assignments.items() if value == branch}
    result = {'page:' + hashlib.sha256((row['url'] + ':' + row['content_hash']).encode()).hexdigest()
              for address, row in pages.items() if address in members
              and row['state'] == 'fetched' and row['content_hash']}
    sources = [source.id for source in Department.query.filter_by(school_id=school_id)
               if source.list_selector and canonical_url(source.list_url) in members]
    result.update('source:' + str(ident) for ident in sources)
    result.update('notice:' + str(ident) for (ident,) in Announcement.query.with_entities(Announcement.id)
                  .filter(Announcement.department_id.in_(sources)))
    return result


class NavigationBranch:
    def __init__(self, inventory, site_key, school_id, scope, page_url):
        self.inventory, self.site_key, self.school_id = inventory, site_key, school_id
        self.scope = str(scope)
        self.url = branch_url(inventory, site_key, page_url)
        fingerprint = hashlib.sha256((self.scope + '\0' + site_key + '\0' + self.url).encode()).hexdigest()
        # Serialize only one branch's paid decisions, never all schools or units.
        self.lock = FileLock(str(inventory.path) + '.navigation-' + fingerprint + '.lock', timeout=0)

    def state(self, *, previous=None, previous_branch_url=None, decision_id=None):
        observed = progress_ids(self.inventory, self.site_key, self.school_id, self.url)
        with self.inventory.connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS navigation_branch_progress ('
                'scope TEXT NOT NULL,site_key TEXT NOT NULL,branch_url TEXT NOT NULL,state_json TEXT NOT NULL,'
                'PRIMARY KEY(scope,site_key,branch_url))')
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT state_json FROM navigation_branch_progress '
                'WHERE scope=? AND site_key=? AND branch_url=?', (self.scope, self.site_key, self.url)).fetchone()
            state = json.loads(row[0]) if row else progress_state(observed)
            if previous and (previous_branch_url is None or canonical_url(previous_branch_url) == self.url):
                # Upgrade old per-page checkpoints once without discarding their
                # paid execution identities. Historical read timing is unknown;
                # preserve recorded empty windows, not borrowed page counters.
                imported = [ident for ident in previous.get('decision_ids', [])
                            if ident not in state['decision_ids']]
                if imported:
                    state['decision_ids'].extend(imported)
                    state.update(decisions=len(state['decision_ids']), checked_decisions=len(state['decision_ids']))
                    if not (observed - set(previous.get('seen', []))):
                        state['no_change_checks'] = max(state['no_change_checks'], previous.get('no_change_checks', 0))
                        state['stopped'] = state['stopped'] or previous.get('stopped', False)
            new_material = observed - set(state['seen'])
            state['seen'] = sorted(set(state['seen']) | observed)
            if state['stopped'] and new_material:
                # A later successful read can resume saved routes in this same
                # generation. Empty polling and another branch cannot do so.
                state.update(stopped=False, no_change_checks=0, checked_decisions=state['decisions'],
                             last_check_count=len(state['seen']))
            if decision_id:
                state = advance_progress(state, decision_id, observed)
            connection.execute('INSERT OR REPLACE INTO navigation_branch_progress VALUES(?,?,?,?)',
                (self.scope, self.site_key, self.url, json.dumps(state, ensure_ascii=False)))
        return state

    def allows(self, execution_id):
        state = self.state()
        if not state['stopped']:
            return True
        # The runtime returns an existing execution without dispatching it.
        # Reserved, sending and unknown plans must keep their original wait or
        # reconciliation path too, rather than being swallowed by this gate.
        from backend.ai.models import AIExecution
        from backend.ai.connection_recovery import resumed_execution_id
        from backend.database.db import db
        return db.session.get(AIExecution, resumed_execution_id(execution_id), populate_existing=True) is not None


def resume_stopped_navigation(inventory, site_key, school_id):
    """A successful new branch read immediately releases its saved decisions."""
    from datetime import datetime, timedelta
    from sqlalchemy import update
    from backend.database.db import db
    from backend.database.models import BackgroundTask
    from backend.services.discovery_control import requested
    discover = BackgroundTask.query.filter_by(identity=f'discover:{school_id}').first()
    resumed = 0
    for job in BackgroundTask.query.filter_by(kind='navigation_review', state='done'):
        payload = job.payload or {}
        if (payload.get('school_id') != school_id or (payload.get('site') or {}).get('site_key') != site_key
                or (job.result or {}).get('status') != 'branch_stopped'):
            continue
        parent = db.session.get(BackgroundTask, payload.get('parent_task_id'))
        if (not parent or parent.generation != payload.get('parent_generation') or requested(parent)
                or (parent.result or {}).get('automatic_discovery_disabled')
                or discover and discover.generation != payload.get('school_generation', payload.get('parent_generation'))):
            continue
        page = payload['page']
        page_url = page.get('snapshot_url', page['url'])
        checkpoint = dict(job.checkpoint or {})
        owner = discover or parent
        branch = NavigationBranch(inventory, site_key, school_id,
                                  f'{school_id}:{owner.id}:{owner.generation}', page_url)
        observed = progress_ids(inventory, site_key, school_id, page_url)
        reassigned = checkpoint.get('navigation_branch_url') not in (None, branch.url)
        if not reassigned and not (observed - set(checkpoint.get('navigation_progress', {}).get('seen', []))):
            continue
        state = branch.state()
        if state['stopped']:
            continue
        checkpoint.update(navigation_progress=state, navigation_branch_url=branch.url)
        current = datetime.utcnow()
        changed = db.session.execute(update(BackgroundTask).where(BackgroundTask.id == job.id,
            BackgroundTask.generation == job.generation, BackgroundTask.state == 'done').values(
                state='pending', phase='ai_resource', available_at=current, queued_at=current,
                deadline_at=max(job.deadline_at or current, current + timedelta(hours=2)),
                finished_at=None, token=None, worker_id=None, lease_until=None, checkpoint=checkpoint,
                error='', error_code='').execution_options(synchronize_session=False))
        resumed += changed.rowcount
    if resumed:
        db.session.commit()
    return resumed
