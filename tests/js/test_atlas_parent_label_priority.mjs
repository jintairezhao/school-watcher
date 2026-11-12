import assert from 'node:assert/strict';
import * as view from '../../frontend/static/js/discovery-atlas-view.js';
const parent={id:'office',depth:99,degree:0,record:{kind:'unit',entity_type:'organization'}};
const child={id:'section',depth:0,degree:20,record:{kind:'unit',entity_type:'organization'}};
const source={id:'source',depth:0,degree:20,record:{kind:'channel',entity_type:'source'}};
const args={selected:null,hovered:null,path:new Set(),context:new Set(),detailed:true};
assert.ok(view.labelImportance(parent,{...args,pixels:1,hierarchyLevel:2})>
    view.labelImportance(child,{...args,pixels:30,hierarchyLevel:3}),
    'A larger or busier child must not take its parent label slot');
const edges=[{from:parent,to:child,type:'organization_membership'},
    {from:child,to:source,type:'source_ownership'}];
const before=JSON.stringify(edges);
const levels=view.labelHierarchyLevels([parent,child,source],edges);
assert.ok(levels.get(parent.id)<levels.get(child.id));
assert.ok(levels.get(child.id)<levels.get(source.id));
assert.deepEqual([...view.labelAncestors(edges,source,null)].sort(),['office','section']);
assert.ok(view.focusContext(edges,source,null).has(parent.id),'Grandparents remain discoverable when a descendant is selected');
assert.equal(view.labelImportance(source,{...args,selected:source,pixels:1,hierarchyLevel:levels.get(source.id)}),1e6);
assert.equal(view.labelImportance(parent,{...args,pixels:20,hierarchyLevel:2,nodeLabels:false}),-1);
const navigation={from:source,to:parent,type:'navigation_link'};
assert.deepEqual(view.labelHierarchyLevels([parent,child,source],[...edges,navigation]),levels,
    'A navigation backlink must not turn into an organization parent');
assert.deepEqual([...view.labelAncestors([navigation],parent,null)],[]);
const cycle=[...edges,{from:child,to:parent,type:'directory_listing'}];
const cyclic=view.labelHierarchyLevels([parent,child,source],cycle);
assert.equal(cyclic.get(parent.id),cyclic.get(child.id),'Cycles receive one display level instead of an invented parent');
assert.ok(cyclic.get(source.id)>cyclic.get(parent.id));
assert.equal(JSON.stringify(edges),before,'Label policy cannot alter real relationships or layout parents');
const page={id:'office-home',record:{kind:'directory',entity_type:'page'}};
const bound=view.labelHierarchyLevels([parent,page,source],[
    {from:parent,to:page,type:'entity_page'},{from:page,to:source,type:'source_on_page'}]);
assert.ok(bound.get(parent.id)<bound.get(page.id),'A department name precedes a bound page even if that page is called a directory');
assert.ok(bound.get(page.id)<bound.get(source.id));
assert.deepEqual([...view.labelAncestors([{from:parent,to:page,type:'entity_page'}],page,null)],[],
    'A display binding is not presented as an organization parent');
console.log('Parent-first labels, typed ancestors, cycles, selection and the total text switch pass');
