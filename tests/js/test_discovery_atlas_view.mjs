import assert from 'node:assert/strict';
import {focusContext, displayLink, labelPolicy, labelImportance} from '../../frontend/static/js/discovery-atlas-view.js';
const root={id:'root',depth:0,record:{kind:'root'}},unit={id:'unit',depth:1,record:{kind:'unit'}},channel={id:'channel',depth:2,record:{kind:'channel'}},other={id:'other',depth:1,record:{kind:'unit'}};
const trunk={from:root,to:unit,primary:true},cross={from:unit,to:channel},outside={from:root,to:other};
const links=[trunk,cross,outside];
assert.equal(displayLink(trunk,null,null),true);
assert.equal(displayLink(cross,null,null),false,'Overview suppresses cross-links, not model evidence');
assert.equal(displayLink(cross,unit,null),true);
assert.equal(displayLink(cross,null,channel),true);
assert.deepEqual([...focusContext(links,channel,null,[root,unit])],['root','unit','channel']);
assert.equal(links.length,3,'Display policy never deletes recorded relationships');
const args={selected:null,hovered:null,path:new Set(),context:new Set(),detailed:false,pixels:2};
assert.ok(labelImportance(unit,args)>0);
assert.equal(labelImportance(channel,args),-1,'Small column names wait for a closer view');
assert.ok(labelImportance(channel,{...args,detailed:true})>=0);
assert.equal(labelImportance(other,{...args,selected:unit,context:new Set(['unit','channel'])}),-1);
assert.equal(labelImportance(channel,{...args,selected:channel}),1e6);
for(const kind of ['root','unit','directory','channel','major','navigation']) {
    const item={id:kind,depth:1,record:{kind}};
    for(const focus of [{},{selected:item},{hovered:item},{path:new Set([item.id])}])
        assert.equal(labelImportance(item,{...args,...focus,detailed:true,nodeLabels:false}),-1,'All node labels can be hidden, including selected and path labels');
}
assert.equal(labelPolicy({width:390,height:800,zoom:1}).limit,7);
assert.ok(labelPolicy({width:1586,height:992,zoom:1}).limit<=42);
assert.ok(labelPolicy({width:1586,height:992,zoom:2}).limit>labelPolicy({width:1586,height:992,zoom:1}).limit);
console.log('Atlas display policy: overview, zoom, local context and unchanged evidence pass');
