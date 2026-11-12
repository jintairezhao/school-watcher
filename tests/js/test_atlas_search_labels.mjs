import assert from 'node:assert/strict';
import {labelImportance,labelHierarchyLevels,searchLabelLevel} from '../../frontend/static/js/discovery-atlas-view.js';
const school={id:'school',label:'测试大学',record:{kind:'root',entity_type:'organization'}};
const directory={id:'directory',label:'院系设置',record:{kind:'directory',entity_type:'directory_group'}};
const first={id:'college-1',label:'人文学院',degree:0,record:{kind:'unit',entity_type:'organization',path:[]}};
const second={id:'college-2',label:'地球科学与工程学院',degree:3,record:{kind:'unit',entity_type:'organization',path:[]}};
const old={id:'old-page',label:'工学院',degree:30,record:{kind:'unit',entity_type:'page',path:[]}};
const child={id:'notice',label:'通知公告',degree:1,record:{kind:'channel',entity_type:'source',path:['人文学院']}};
const page={id:'home',label:'人文学院',record:{kind:'unit',entity_type:'page',path:[]}};
const edges=[{from:school,to:directory,type:'directory_listing'},
    {from:directory,to:first,type:'directory_listing'},
    {from:directory,to:second,type:'directory_listing'},
    {from:first,to:page,type:'entity_page'},
    {from:first,to:child,type:'source_ownership'}];
const levels=labelHierarchyLevels([school,directory,first,second,old,child,page],edges);
const base={selected:null,hovered:null,path:new Set(),context:new Set(),detailed:false,pixels:.25,upstream:new Set()};
const importance=(node,query,extra={})=>labelImportance(node,{...base,query,hierarchyLevel:levels.get(node.id),...extra});
assert.equal(levels.get(first.id),levels.get(second.id),'Peer colleges must retain the same recorded display tier');
assert.ok(importance(first,'学院')>importance(directory,'学院'),
    'A matching college must get a label before its nonmatching navigation context');
assert.ok(importance(second,'学院')>importance(old,'学院'),
    'A proved organization must precede a busy unbound old navigation page');
assert.ok(importance(child,'通知公告')>=0,'An explicit tiny search match is readable without zooming');
assert.ok(importance(first,'学院',{selected:school,context:new Set(['school'])})>=0,
    'An unrelated earlier selection must not suppress the matching colleges');
assert.ok(importance(first,'学院')>importance(child,'学院'),
    'Own-name matches precede a child which only inherits matching path text');
assert.ok(importance(first,'人文学院')>importance(page,'人文学院'),
    'Matching organization precedes its bound webpage');
assert.equal(importance(first,'学院',{nodeLabels:false}),-1,'The text switch still hides search labels');
assert.equal(importance(child,''),-1,'Without a query, tiny source labels retain the overview policy');
assert.equal(importance(first,'学院',{selected:first}),1e6);
const searchLevel=searchLabelLevel([school,directory,first,second,old,child,page],levels,'学院');
assert.equal(searchLevel,levels.get(first.id));
assert.ok(importance(second,'学院',{searchLevel})>=0);
assert.equal(importance(old,'学院',{searchLevel}),-1,'Unbound page names do not compete with matched organizations in the search overview');
assert.ok(importance(old,'学院',{searchLevel,detailed:true})>=0,'Zoom still reveals actual lower-level records');
assert.ok(importance(old,'工学院',{searchLevel:searchLabelLevel([old],levels,'工学院')})>=0,
    'An explicit old-name lookup still exposes the recorded page; identities are not renamed or deleted');
console.log('Search matches, peer colleges, ancestry context, previous selection and total text switch pass');
