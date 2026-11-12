import assert from 'node:assert/strict';
import fs from 'node:fs';
import {buildAtlas,accessRoute} from '../../frontend/static/js/discovery-atlas-model.js';

const fixture=JSON.parse(fs.readFileSync(new URL('../fixtures/structure/cupk-multi-page-entity-access-20261108.json',import.meta.url),'utf8'));
const original=JSON.stringify(fixture.snapshot);
const model=buildAtlas(fixture.snapshot,{deferLayout:true});
for(const sample of fixture.cases){
    const item=model.actual.get(sample.id),route=accessRoute(model,item);
    const bindings=model.links.filter(edge=>edge.type==='entity_page'&&edge.from===item);
    const pages=[...new Set(bindings.map(edge=>edge.to))];
    const reachable=pages.map(page=>accessRoute(model,page)).filter(path=>path.complete);
    assert.ok(reachable.length,'The retained subset has a real forward route for '+sample.label);
    assert.equal(route.complete,true,sample.label+' must not lose its route because it has multiple recorded pages');
    assert.equal(route.steps.length,Math.min(...reachable.map(path=>path.steps.length)));
    assert.deepEqual(new Set(route.hostingPages.map(page=>page.id)),new Set(pages.map(page=>page.id)));
    assert.deepEqual(new Set(route.entityPageRelations.map(edge=>edge.id)),new Set(bindings.map(edge=>edge.id)));
    assert.ok(route.steps.every(step=>step.edge.type==='navigation_link'&&step.edge.from===step.from));
    assert.equal(route.terminalGap,'entity_page_not_click');
    assert.ok(!route.links.includes(route.entityPageRelation),'Entity correspondence is not a website click');
    assert.equal(route.equivalentHostingPages,undefined,'A department having several pages does not make their URLs equivalent');
}
assert.equal(JSON.stringify(fixture.snapshot),original,'Choosing an entrance cannot change facts or evidence');

const url='https://school.example.edu.cn/';
const nodes=[
    {id:'root',entity_type:'page',kind:'root',url,label:'Root'},
    {id:'a',entity_type:'page',url:url+'a/',label:'Primary website'},
    {id:'b',entity_type:'page',url:url+'b/',label:'Second website'},
    {id:'middle',entity_type:'page',url:url+'middle/',label:'Directory'},
    {id:'organization',entity_type:'organization',url:url+'a/',label:'Recorded department'},
];
const evidence=[{reference_url:url,locator:'nav a',content_hash:'saved-fixture',observed_at:'2026-11-08T00:00:00Z'}];
const edge=(source,target,type,observed_relation)=>({source,target,type,observed_relation,evidence,access:type==='navigation_link'});
const bindings=[edge('organization','a','entity_page'),edge('organization','b','entity_page')];
const routeFor=relations=>{const m=buildAtlas({nodes,relations},{deferLayout:true});return accessRoute(m,m.actual.get('organization'));};

const oneReachable=routeFor([...bindings,edge('root','b','navigation_link')]);
assert.equal(oneReachable.complete,true);
assert.equal(oneReachable.hostingPage.id,'b','An unreachable preferred website cannot hide a reachable alternative');
assert.equal(oneReachable.hostingPages.length,2);

const shortest=routeFor([...bindings,edge('root','b','navigation_link'),edge('root','middle','navigation_link'),edge('middle','a','navigation_link')]);
assert.equal(shortest.hostingPage.id,'b','A direct entrance wins over the longer preferred website route');

const equal=[...bindings,edge('root','a','navigation_link'),edge('root','b','navigation_link')];
assert.equal(routeFor(equal).hostingPage.id,'a');
assert.equal(routeFor([...equal].reverse()).hostingPage.id,'a','Equal-length selection is independent of input order');

const reverseOnly=routeFor([...bindings,edge('a','root','navigation_link'),edge('b','root','navigation_link')]);
assert.equal(reverseOnly.complete,false,'Reversing a website link cannot manufacture an entrance');
assert.ok(!reverseOnly.steps.some(step=>step.edge.type==='entity_page'));
assert.equal(reverseOnly.hostingPages.length,2);

const unbound=routeFor([edge('root','a','navigation_link')]);
assert.equal(unbound.complete,false);
assert.equal(unbound.gap,'entity_page_unknown','A matching URL does not manufacture entity correspondence');

const duplicate=routeFor([edge('organization','a','entity_page'),edge('organization','a','entity_page','verified_directory_website'),edge('root','a','navigation_link')]);
assert.equal(duplicate.complete,true);
assert.equal(duplicate.hostingPages.length,1);
assert.equal(duplicate.entityPageRelations.length,2,'Different original binding predicates remain inspectable');
console.log('Multi-page entity routes: four retained cases and directed-route boundaries passed');
