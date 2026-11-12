"""Read legacy unions as scoped previews, never as replacement subscriptions."""
from copy import deepcopy
import hashlib
import json

from backend.services.source_inventory import canonical_url

STATUS_LABELS={'resolved':'已核实预览关联','target_not_configured':'完整栏目尚未接入',
    'target_unverified':'完整栏目待验证','target_material_missing':'缺少栏目原文',
    'target_identity_ambiguous':'存在多个候选栏目','preview_items_not_confirmed':'预览条目尚未对应'}

def load_joint_scope_observations(school_id, departments, pages, material):
    from backend.database.db import db
    from backend.database.source_governance_models import SourceProposal, SourceReviewEvent
    from backend.services import source_governance as g
    from backend.services.source_scope import observe_joint_selection
    sources={s['id']:s for s in departments}
    indexed={canonical_url(p['url']):p for p in pages}
    result=[];seen=set()
    with db.session.no_autoflush:
        rows=db.session.query(SourceReviewEvent,SourceProposal).join(SourceProposal,
            SourceProposal.id==SourceReviewEvent.proposal_id).filter(SourceProposal.school_id==school_id,
            SourceReviewEvent.action=='configured_scope_preserved').order_by(SourceReviewEvent.id.desc()).all()
    for event,proposal in rows:
        source=sources.get(proposal.department_id)
        if not source or source['id'] in seen:continue
        config={f:source.get(f) or '' for f in g.FIELDS}
        saved=json.loads(event.detail_json).get('configured_scope') or {}
        if (saved.get('config_hash')!=g._hash(config) or proposal.expected_config_hash!=g._hash(config)
                or g._hash(json.loads(proposal.evidence_json))!=proposal.evidence_hash):continue
        seen.add(source['id'])
        address=canonical_url(config['list_url']);page=indexed.get(address) or {}
        html=material(address) or ''
        if (page.get('state')!='fetched' or not html or g._hash(html)!=page.get('content_hash')):
            result.append(dict(source_id=source['id'],config_hash=g._hash(config),
                kind=saved.get('kind','configured_multiple_regions'),status='scope_material_missing',regions=[]))
            continue
        reference=dict(url=address,hash=page['content_hash'],observed_at=page.get('checked_at'),outcome='usable')
        retained=saved.get('material') or {}
        if retained.get('hash')==reference['hash'] and retained.get('script_materials'):
            reference['script_materials']=retained['script_materials']
            try:html=g.read_snapshot(reference)
            except (ValueError,OSError):pass  # Plain original DOM keeps unknown tab labels unknown.
        observed=observe_joint_selection(config,reference,html)
        if observed:result.append(dict(observed,source_id=source['id']))
    return result


def resolve_scope_components(*, scopes, pages, departments, receipts, material):
    from backend.services import source_governance as g
    from backend.scraper.discovery.structure import locator
    indexed={canonical_url(p['url']):p for p in pages}
    sources={s['id']:s for s in departments}
    current={r['source_id']:r for r in receipts}
    documents={};result=[]

    def document(url):
        if url not in documents:
            page=indexed.get(url) or {};html=material(url) or ''
            documents[url]=(html,page) if (page.get('state')=='fetched' and page.get('checked_at')
                and html and hashlib.sha256(html.encode()).hexdigest()==page.get('content_hash')) else None
        return documents[url]

    def proof(url,node,page,role):
        return dict(reference_url=url,content_hash=page['content_hash'],observed_at=page['checked_at'],
                    locator=locator(node),role=role)

    for scope in scopes:
        source=sources.get(scope['source_id'])
        if not source:continue
        config={f:source.get(f) or '' for f in g.FIELDS}
        if scope.get('config_hash')!=g._hash(config):continue
        observed=deepcopy(scope);observed['components']=[]
        address=canonical_url(config['list_url']);home=document(address)
        if not home or scope.get('status')=='scope_material_missing':
            observed['status']='scope_material_missing';result.append(observed);continue
        for region in scope.get('regions',[]):
            entry=canonical_url(region.get('column_url') or '')
            component=dict(name=region['name'],column_url=entry,preview_items=region['selected_items'],
                full_column_equivalent=False,status='entry_unconfirmed')
            observed['components'].append(component)
            if (region.get('name_status')!='observed' or not region.get('complete_region')
                    or not entry or not region.get('column_link')):continue
            candidates=[s for s in departments if s['id']!=source['id']
                and s.get('school_id')==source.get('school_id') and s.get('kind','column')=='column'
                and canonical_url(s.get('list_url') or '')==entry and s.get('list_selector')]
            component['candidate_source_ids']=[s['id'] for s in candidates]
            if not candidates:component['status']='target_not_configured';continue
            validated=[]
            for target in candidates:
                cfg={f:target.get(f) or '' for f in g.FIELDS};receipt=current.get(target['id'])
                if (receipt and receipt.get('config_hash')==g._hash(cfg) and receipt.get('url')==entry
                        and receipt.get('receipt_id')):validated.append((target,cfg,receipt))
            if not validated:component['status']='target_unverified';continue
            column=document(entry)
            if not column:component['status']='target_material_missing';continue
            preview_cfg=dict(config,name=region['name'],list_selector=region['list_selector'])
            home_soup,preview_nodes,preview,errors=g._extract(preview_cfg,{'url':address},html=home[0])
            if errors or len(preview)!=region['selected_items']:
                component['status']='preview_extraction_incomplete';continue
            matches=[]
            for target,cfg,receipt in validated:
                records,errors,target_region=g._column_scope(cfg,{'url':entry},html=column[0])
                if (errors or not records or not g._column_matches(region['name'],target_region.get('name',''))):continue
                soup,nodes,records,errors=g._extract(cfg,{'url':entry},html=column[0])
                record_nodes={(canonical_url(r['url']),g._normal(r['title'])):node for r,node in zip(records,nodes)}
                pairs=[(r,node,record_nodes.get((canonical_url(r['url']),g._normal(r['title']))))
                       for r,node in zip(preview,preview_nodes)]
                if not pairs or not all(pair[2] is not None for pair in pairs):continue
                heading=home_soup.select_one(region['heading']);link=home_soup.select_one(region['column_link'])
                container=home_soup.select_one(region['container'])
                target_heading=soup.select_one(target_region['heading']);target_container=soup.select_one(target_region['container'])
                if any(n is None for n in (heading,link,container,target_heading,target_container)):continue
                evidence=[proof(address,heading,home[1],'preview_heading'),proof(address,link,home[1],'preview_entry'),
                    proof(address,container,home[1],'preview_container'),proof(entry,target_heading,column[1],'column_heading'),
                    proof(entry,target_container,column[1],'column_container')]
                for record,preview_node,column_node in pairs:
                    evidence.extend([proof(address,preview_node,home[1],'preview_item'),proof(entry,column_node,column[1],'column_item')])
                matches.append(dict(target_source_id=target['id'],target_config_hash=g._hash(cfg),
                    receipt_id=receipt['receipt_id'],matched_items=len(pairs),evidence=evidence,
                    **({'tab_binding_evidence':region['tab_binding_evidence']} if region.get('tab_binding_evidence') else {})))
            if len(matches)==1:component.update(matches[0],status='resolved')
            else:component['status']='target_identity_ambiguous' if matches else 'preview_items_not_confirmed'
        observed['status']='resolved' if observed['components'] and all(c['status']=='resolved' for c in observed['components']) else 'scope_incomplete'
        for component in observed['components']:
            component['status_label']=STATUS_LABELS.get(component['status'],'关联依据待补充')
        result.append(observed)
    return result
