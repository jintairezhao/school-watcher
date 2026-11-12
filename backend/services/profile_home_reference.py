"""Prove a distinct, explicitly linked homepage without merging page identities."""
import hashlib
import re
import unicodedata
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .source_inventory import resolve_page_link


def _homepage_identity(relations,page,unit_name):
    from .publisher_identity import DECLARATION,saved_observation
    from .source_relationships import SourceRelationships
    nodes=[dict(kind='unit',relation=DECLARATION,node_key=n['key'],name=n['name'])
        for n in saved_observation(page,relations.site['site_key']).get('nodes',[])
        if n.get('relation')==DECLARATION and n.get('name')==unit_name and n.get('key')]
    owner=dict(unit_name=unit_name,unit_key=nodes[0]['node_key']if nodes else '',nodes=nodes)
    proof=SourceRelationships._unit_website_identity(relations,page,owner)
    return {key:proof[key] for key in ('source','identity','locator')} if proof else None


def _qualified_brand_name(unit_name):
    """One explicit office qualifier; this is not an organization alias rule."""
    value=unicodedata.normalize('NFKC',unit_name or'').strip()
    match=re.fullmatch(r'([^()]+)\(([^()]+(?:办公室|学院|研究院|研究所|中心|学部|部门))\)',value)
    return match[1].strip()if match else None


def _profile_bound_home_identity(relations,page,profile,unit_name):
    from .publisher_identity import saved_observation
    from .source_ownership import profile_subject_matches
    name=_qualified_brand_name(unit_name)
    claims=saved_observation(profile,relations.site['site_key']).get('profile_names')or[]
    if (not name or not claims or not all(profile_subject_matches(c.get('entity_name'),
            unit_name, relations.site['name']) for c in claims)):
        return None
    exact=[c for c in claims if c.get('source')=='unit_profile_self_description'and c.get('locator')]
    brand=_homepage_identity(relations,page,name)
    if len(exact)!=1 or not brand:
        return None
    claim=exact[0]
    proof=dict(source=claim['source'],entity_name=claim['entity_name'],locator=claim['locator'],
        reference_url=profile['url'],content_hash=profile['content_hash'],observed_at=profile['checked_at'])
    return brand,proof


def _literal_unit_title(value):
    return isinstance(value,str) and bool(re.fullmatch(
        r'[\u3400-\u9fffA-Za-z0-9·（）()\s]{2,90}(?:学院|中心|研究院|研究所|学部|部门|办公室|图书馆|院|部|处)',
        value.strip()))


def _shared_title_profile_identity(relations,entry,page,profile,unit_name):
    """Keep a literal shared brand distinct from the profile's full subject."""
    from .publisher_identity import saved_observation
    from .source_ownership import normalized_name,profile_subject_matches
    title=entry.get('title')or''
    if (not _literal_unit_title(title) or normalized_name(title)==normalized_name(relations.site['name'])
            or normalized_name(page.get('title'))!=normalized_name(title)):
        return None
    claims=saved_observation(profile,relations.site['site_key']).get('profile_names')or[]
    exact=[c for c in claims if c.get('source')=='unit_profile_self_description'and c.get('locator')]
    if (len(exact)!=1 or not claims or not all(profile_subject_matches(c.get('entity_name'),
            unit_name,relations.site['name'])for c in claims)):
        return None
    entry_brand=_homepage_identity(relations,entry,title)
    brand=_homepage_identity(relations,page,title)
    if not entry_brand or not brand or entry_brand.get('source')!='title'or brand.get('source')!='title':
        return None
    claim=exact[0]
    name=dict(source=claim['source'],entity_name=claim['entity_name'],locator=claim['locator'],
        reference_url=profile['url'],content_hash=profile['content_hash'],observed_at=profile['checked_at'])
    shared=dict(kind='same_literal_homepage_title',reference_url=entry['url'],identity=entry_brand,
        content_hash=entry['content_hash'],observed_at=entry['checked_at'])
    return brand,name,shared


def home_return(relations,home,profile,unit_name):
    """A profile may return to another page, but every leg needs retained evidence."""
    from .publisher_identity import is_home_route,routes,profile_access_routes
    from .source_placements import _within_unit
    backs=[r for r in routes(profile,relations.site['site_key']) if is_home_route(r) and r.get('locator')]
    direct=next((r for r in backs if r.get('target_url')==home['url']),None)
    if direct:
        return dict(profile_home_locator=direct['locator'])
    for back in backs:
        target=back.get('target_url');page=relations.pages.get(target)or{}
        if (page.get('state')!='fetched' or not page.get('content_hash')or not page.get('checked_at')
                or not _within_unit(target,home['url'],relations.site['root_url'])):
            continue
        entrance=next((r for r in routes(home,relations.site['site_key'])
            if is_home_route(r) and r.get('target_url')==target and r.get('locator')),None)
        if not entrance:
            continue
        route=next((r for r in profile_access_routes(relations,page,scope_url=home['url'])
            if r['profile_url']==profile['url']),None)
        if route:
            identity=_homepage_identity(relations,page,unit_name)
            name_witness=shared_title=None
            if not identity:
                bound=_profile_bound_home_identity(relations,page,profile,unit_name)
                if bound:
                    identity,name_witness=bound
                else:
                    shared=_shared_title_profile_identity(relations,home,page,profile,unit_name)
                    if not shared:continue
                    identity,name_witness,shared_title=shared
            return dict(profile_home_locator=back['locator'],profile_home_witness=dict(
                homepage_url=target,entry_locator=entrance['locator'],identity=identity,profile_route=route['hops'],
                **({'profile_name_witness':name_witness}if name_witness else{}),
                **({'shared_title_witness':shared_title}if shared_title else{})))
    return None


def return_references(relations,home,witness):
    if not witness:
        return []
    name=witness.get('profile_name_witness')or{}
    shared=witness.get('shared_title_witness')or{}
    return ([relations._reference(home,[witness['entry_locator']]),
        relations._reference(witness['homepage_url'],[witness['identity']['locator']])]+
        [relations._reference(hop['reference_url'],[hop['locator']]) for hop in witness['profile_route']]+
        ([relations._reference(name['reference_url'],[name['locator']])]if name else[])+
        ([relations._reference(home,[shared['identity']['locator']])]if shared else[]))


def verify_home_return(home,profile,back_locator,witness,unit_name,relations,*,material=None):
    """The projection checks the same witness; a writer additionally checks raw DOMs."""
    from .publisher_identity import home_anchor_kind,checked_profile_route,verify_profile_route,saved_observation
    from .source_ownership import branding_candidates,normalized_name,website_identity_forms,profile_subject_matches
    from .source_placements import _within_unit
    from backend.scraper.discovery.structure import locator_element
    if not isinstance(witness,dict)or not isinstance(witness.get('homepage_url'),str):
        return None
    entry,description=relations.pages.get(home),relations.pages.get(profile)
    if not entry or not description:
        return None
    index=witness['homepage_url'];trail=witness.get('profile_route')
    identity=witness.get('identity')or{}
    page=relations.pages.get(index)or{}
    names={normalized_name(unit_name),normalized_name(relations.site.get('name',''))+normalized_name(unit_name)}
    named=witness.get('profile_name_witness')
    shared=witness.get('shared_title_witness')
    if shared is not None:
        entry_brand=shared.get('identity')if isinstance(shared,dict)else None
        title=(entry or{}).get('title')or''
        if (not isinstance(shared,dict)or shared.get('kind')!='same_literal_homepage_title'
                or not isinstance(entry_brand,dict)or entry_brand.get('source')!='title'
                or not isinstance(identity,dict)or identity.get('source')!='title'
                or named is None or not _literal_unit_title(title)
                or normalized_name(title)==normalized_name(relations.site.get('name',''))
                or shared.get('reference_url')!=home
                or shared.get('content_hash')!=(entry or{}).get('content_hash')
                or shared.get('observed_at')!=(entry or{}).get('checked_at')
                or not isinstance(entry_brand.get('locator'),str)or not entry_brand['locator']
                or normalized_name(entry_brand.get('identity'))!=normalized_name(title)
                or normalized_name(identity.get('identity'))!=normalized_name(title)
                or normalized_name(page.get('title'))!=normalized_name(title)):
            return None
        names={normalized_name(title)}
    if named is not None:
        brand_name=_qualified_brand_name(unit_name)
        claims=saved_observation(description or{},relations.site['site_key']).get('profile_names')or[]
        if (not isinstance(named,dict)or not shared and not brand_name
                or named.get('reference_url')!=profile
                or named.get('content_hash')!=(description or{}).get('content_hash')
                or named.get('observed_at')!=(description or{}).get('checked_at')
                or named.get('source')!='unit_profile_self_description'
                or not profile_subject_matches(named.get('entity_name'),unit_name,relations.site['name'])
                or not isinstance(named.get('locator'),str)or not named['locator']
                or not claims or not all(profile_subject_matches(c.get('entity_name'),
                    unit_name,relations.site['name'])for c in claims)
                or not any(c.get('source')=='unit_profile_self_description'for c in claims)):
            return None
        if not shared:
            names={normalized_name(brand_name),normalized_name(relations.site.get('name',''))+normalized_name(brand_name)}
    if (index==home or not _within_unit(index,home,relations.site['root_url'])
            or not isinstance(identity,dict)or not isinstance(identity.get('identity'),str)
            or not isinstance(trail,list)or not trail or not isinstance(trail[0],dict)
            or not all(isinstance(p,str)and p for p in (back_locator,witness.get('entry_locator'),identity.get('locator')))
            or identity.get('source')not in ('title','og:site_name','header_logo_alt','footer_signature')
            or not website_identity_forms(identity.get('identity'))&names
            or any(p.get('state')!='fetched'or not p.get('content_hash')or not p.get('checked_at')
                for p in (entry,description,page))):
        return None
    checked=checked_profile_route(index,profile,trail[0].get('locator'),trail,relations.pages,
        relations.site['root_url'],scope_url=home)
    if not checked:
        return None
    locations=[(home,witness['entry_locator']),(profile,back_locator),(index,witness['identity']['locator'])]
    if named:locations.append((profile,named['locator']))
    if shared:locations.append((home,shared['identity']['locator']))
    locations += [(h['reference_url'],h['locator'])for h in checked]
    # Published projections intentionally omit large parser notes. They check
    # the stored witness and each current material hash; only writers replay DOMs.
    if material is not None:
        expected=home_return(relations,entry,description,unit_name)
        if not expected or expected.get('profile_home_witness')!=witness or expected['profile_home_locator']!=back_locator:
            return None
        documents={}
        for url,_ in locations:
            if url in documents:
                continue
            raw=material(url)
            if not raw or hashlib.sha256(raw.encode()).hexdigest()!=relations.pages[url]['content_hash']:
                return None
            documents[url]=BeautifulSoup(raw,'lxml')
        for origin,position in ((home,witness['entry_locator']),(profile,back_locator)):
            node=locator_element(documents[origin],position)
            if not home_anchor_kind(node):
                return None
            base=documents[origin].find('base',href=True)
            response=relations.pages[origin].get('final_url')or origin
            try:
                if resolve_page_link(urljoin(response,base['href'])if base else response,node['href'])!=index:
                    return None
            except ValueError:
                return None
        if not any(all(candidate.get(k)==v for k,v in witness['identity'].items())
                for candidate in branding_candidates(documents[index])):
            return None
        if shared and not any(all(candidate.get(k)==v for k,v in shared['identity'].items())
                for candidate in branding_candidates(documents[home])):
            return None
        if named:
            from .source_ownership import profile_identity_candidates
            if not any(all(claim.get(k)==named[k]for k in ('source','entity_name','locator'))
                    for claim in profile_identity_candidates(documents[profile])):
                return None
        if not verify_profile_route(index,profile,trail[0]['locator'],trail,material,relations,scope_url=home):
            return None
    return [dict(reference_url=url,locator=position,content_hash=relations.pages[url]['content_hash'],
        observed_at=relations.pages[url]['checked_at'])for url,position in dict.fromkeys(locations)]
