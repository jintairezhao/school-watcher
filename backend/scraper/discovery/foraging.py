"""Evidence-bound path hints within one profile/coverage scheduling tier.

Receipts come from source_path_receipts, not model proposals. These hints are
neither ownership claims nor estimated probabilities, token costs or seconds.
The main scheduler retains control of profile coverage and fair exploration.
"""
from collections import defaultdict
from fractions import Fraction
import re

from backend.services.source_inventory import canonical_url
from backend.scraper.discovery.attention import complete_material


def _url(value):
    if not isinstance(value, str):
        return ''
    try:
        return canonical_url(value)
    except (ValueError, UnicodeError):
        return ''


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


class ForagingFeedback:
    """Read one current observed graph and its already executed source receipts."""

    def __init__(self, history=(), edges=(), receipts=(), branches=None, *, unseen_prior=1,
                 path_credit_weight=1, costs=None):
        if type(unseen_prior) is not int or unseen_prior <= 0:
            raise ValueError('unseen_prior must be a positive integer')
        if type(path_credit_weight) is not int or path_credit_weight < 0:
            raise ValueError('path_credit_weight must be a non-negative integer')
        self.unseen_prior, self.path_credit_weight = unseen_prior, path_credit_weight
        self.history = {_url(url): dict(page, url=_url(url)) for url, page in dict(history).items()
                        if _url(url) and isinstance(page, dict) and _url(page.get('url', url)) == _url(url)}
        self.parents, self.targets = defaultdict(set), defaultdict(set)
        self.credit, self.sources, self.material = set(), defaultdict(set), defaultdict(set)
        for edge in edges:
            parent_url, target_url = _url(edge.get('parent_url')), _url(edge.get('target_url'))
            parent, target = self.history.get(parent_url), self.history.get(target_url)
            if (not parent or not target or not self.complete(parent)
                    or edge.get('decision') not in ('follow', 'official_external_link')
                    or parent['content_hash'] != edge.get('content_hash')
                    or parent_url == target_url
                    or not isinstance(parent.get('depth'), int) or not isinstance(target.get('depth'), int)
                    or parent['depth'] >= target['depth']):
                continue
            self.parents[target_url].add(parent_url)
            self.targets[parent_url].add(target_url)

        # Derive scope from actual forward placement, never host/path/name.
        # An optional caller cache cannot widen the proven scope.
        self.scopes = {}
        for url, page in sorted(self.history.items(), key=lambda pair: (pair[1].get('depth', 0), pair[0])):
            parents = self.parents[url]
            inherited = {self.scopes[parent] for parent in parents if parent in self.scopes}
            own = (page.get('kind') in ('root', 'unit', 'directory') or len(parents) != 1
                   or len(inherited) != 1
                   or any(self.history[parent].get('kind') == 'root' for parent in parents))
            scope = url if own else next(iter(inherited))
            cached = _url((branches or {}).get(url, scope))
            self.scopes[url] = scope if cached == scope else url
            if self.complete(page):
                # One material per observed address, even if normal updates
                # replace its HTML hash repeatedly. This is not a cost counter.
                self.material[self.scopes[url]].add(url)

        accepted = {}
        conflicted = set()
        for receipt in receipts:
            if not self._receipt_matches(receipt):
                continue
            source_id = receipt['source_id']
            identity = (receipt['url'], receipt['content_hash'], receipt['config_hash'])
            if source_id in accepted and accepted[source_id][0] != identity:
                conflicted.add(source_id)
            else:
                accepted[source_id] = (identity, receipt)
        for source_id, (_, receipt) in accepted.items():
            if source_id in conflicted:
                continue
            url = receipt['url']
            scope = self.scopes[url]
            self.sources[scope].add(source_id)
            # Walk only the unique real trail. Stop credit at scope boundaries:
            # one college cannot make every child of the school directory win.
            while self.scopes.get(url) == scope and url not in self.credit:
                self.credit.add(url)
                parents = self.parents[url]
                if len(parents) != 1:
                    break
                url = next(iter(parents))
        # Aggregate once for this immutable observed graph. The hot rank path
        # must not rebuild trails or rescan a large branch for each candidate.
        self.material_counts = {scope: len(urls) for scope, urls in self.material.items()}
        self.source_counts = {scope: len(ids) for scope, ids in self.sources.items()}
        self.entry_counts = {}
        for scope, materials in self.material.items():
            self.entry_counts[scope] = len({target for material in materials
                for target in self.targets[material] if self.scopes.get(target) == scope})
        self.credited = self.credit | {url for url, parents in self.parents.items()
            if len(parents) == 1 and any(parent in self.credit
                and self.scopes.get(parent) == self.scopes.get(url)
                and self.history[parent].get('kind') != 'root' for parent in parents)}
        from .cost_feedback import CostFeedback
        self.costs = CostFeedback(self.scopes, costs)

    @staticmethod
    def complete(page):
        return complete_material(page)

    def _receipt_matches(self, receipt):
        if not isinstance(receipt, dict):
            return False
        url = _url(receipt.get('url'))
        page = self.history.get(url)
        region = receipt.get('region')
        return bool(url == receipt.get('url') and page and self.complete(page)
                    and isinstance(receipt.get('source_id'), int) and not isinstance(receipt['source_id'], bool)
                    and receipt['source_id'] > 0 and _hash(receipt.get('config_hash'))
                    and isinstance(receipt.get('receipt_id'), str)
                    and re.fullmatch(r'(?:native|proposal):[1-9][0-9]*', receipt['receipt_id'])
                    and receipt.get('content_hash') == page['content_hash']
                    and isinstance(region, dict) and not region.get('empty')
                    and all(isinstance(region.get(key), str) and region[key].strip()
                            for key in ('container', 'heading')))

    def observed_path(self, page):
        """Explain the full unique forward trail, including real layout nodes."""
        url = _url(page.get('url'))
        if url not in self.history:
            return [], 'page_not_observed'
        trail, seen = [], set()
        while url not in seen:
            seen.add(url)
            observed = self.history[url]
            trail.append({'url': url, 'kind': observed.get('kind'), 'label': observed.get('label', '')})
            if observed.get('kind') == 'root':
                return list(reversed(trail)), 'root_reached'
            parents = self.parents[url]
            if len(parents) != 1:
                return list(reversed(trail)), 'ambiguous_parents' if parents else 'no_current_forward_parent'
            url = next(iter(parents))
        return list(reversed(trail)), 'cycle_detected'

    def features(self, page):
        url = _url(page.get('url'))
        scope = self.scopes.get(url, url)
        path, reason = self.observed_path(page)
        return {'scope_url': scope, 'path_credit': url in self.credited,
                'verified_source_count': self.source_counts.get(scope, 0),
                'complete_material_count': self.material_counts.get(scope, 0),
                'observed_forward_entry_count': self.entry_counts.get(scope, 0),
                'observed_path': path, 'path_reason': reason,
                **self.costs.features(url)}

    def rank(self, page):
        """Bounded path hint with neutral optimism for unexplored branches.

        Distinct complete materials diminish repeated probing of a worked
        branch. They measure observed breadth, never actual time or API cost.
        Source/article counts cannot increase the bounded 0/1 path credit.
        """
        url = _url(page.get('url'))
        reads = self.material_counts.get(self.scopes.get(url, url), 0)
        prior = self.unseen_prior
        score = Fraction(prior + int(url in self.credited) * self.path_credit_weight,
                         prior + reads)
        return (-score, reads)
