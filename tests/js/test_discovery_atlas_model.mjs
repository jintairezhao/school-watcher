import assert from 'node:assert/strict';
import {buildAtlas, ancestors, visibleEntries, trackingTarget} from '../../frontend/static/js/discovery-atlas-model.js';

const node = (id, label, path, kind = 'channel', state = 'fetched') =>
    ({id, label, path, kind, state, url:'https://example.edu.cn/' + id});
const school = {name:'演示大学', url:'https://example.edu.cn'};
const records = [
    node('orphan','路径未知栏目',[]),
    node('root','学校官网',[],'root'),
    node('org','组织机构',['学校官网'],'directory'),
    node('faculties','院系设置',['学校官网'],'directory'),
    node('foreign','外国语学院',['学校官网','院系设置'],'unit'),
    node('unit','研究生院',['学校官网','组织机构'],'unit'),
    node('channel-a','通知公告',['学校官网','组织机构','研究生院'],'channel','connected'),
    node('channel-b','通知公告',['学校官网','组织机构','研究生院'],'channel','failed'),
    node('other','通知公告',['学校官网','院系设置','外国语学院'],'channel','pending')
];
const edge=(source,target,type='navigation')=>({source,target,type,evidence_url:'https://example.edu.cn/'+source});
const relations=[edge('root','org','directory'),edge('root','faculties','directory'),edge('org','unit','directory'),
    edge('faculties','foreign','directory'),edge('unit','channel-a'),edge('unit','channel-b'),edge('foreign','other')];
const model = buildAtlas({school, nodes:records,relations});
assert.equal(model.actual.size, records.length, 'Same-named pages on different URLs must remain distinct');
assert.equal(model.actual.get('orphan').parent, null, 'Unknown paths must not acquire a school-root edge');
assert.deepEqual(ancestors(model.actual.get('unit')).map(item => item.label), ['学校官网','组织机构','研究生院']);
assert.equal(model.actual.get('channel-a').parent, model.actual.get('channel-b').parent);
assert.notEqual(model.actual.get('channel-a'), model.actual.get('channel-b'));
assert.notEqual(model.actual.get('channel-a').parent, model.actual.get('other').parent);
assert.ok([...model.entries.values()].every(item => item.record?.url), 'Only URL-bearing recorded pages become nodes');
assert.ok(model.actual.get('root').radius > model.actual.get('unit').radius);
assert.ok(model.actual.get('unit').radius > model.actual.get('channel-a').radius);
assert.ok(new Set([...model.entries.values()].map(item => item.position.z)).size > 2, 'Layout must occupy genuine spatial depth');
const collapsed = new Set([model.actual.get('unit').id]);
assert.ok(!visibleEntries(model, {collapsed}).includes(model.actual.get('channel-a')));
assert.ok(visibleEntries(model, {state:'failed',collapsed}).includes(model.actual.get('channel-b')), 'Filtering reveals matching descendants with their ancestors');
assert.deepEqual(visibleEntries(model, {query:'路径未知'}).map(item => item.record?.id).filter(Boolean), ['orphan']);
assert.equal(visibleEntries(model, {query:'missing'}).length, 0);
const reversed = buildAtlas({school,nodes:[...records].reverse(),relations:[...relations].reverse()});
assert.deepEqual(model.actual.get('root').position, reversed.actual.get('root').position, 'Unplaced pages must not shift the school root');
assert.deepEqual([...model.actual].map(([id,n])=>[id,n.position]).sort(),[...reversed.actual].map(([id,n])=>[id,n.position]).sort(),
    'Input order must not change the spatial layout when a snapshot refreshes');
assert.equal(model.links.filter(link=>link.primary).length,7,'The display backbone uses only the evidenced spanning forest');
assert.ok(model.actual.get('unit').salience>model.actual.get('channel-a').salience,'Overview salience must distinguish the real structure from its individual columns');
assert.equal(buildAtlas({school,nodes:[]}).entries.size, 0);
const collegeNodes=[node('college-group','院系设置',[school.name],'directory'),
    node('college','化工与环境学院',[school.name,'院系设置'],'unit'),
    node('college-notices','通知公告',[school.name,'院系设置','化工与环境学院'],'channel','connected')];
const college=buildAtlas({school,nodes:collegeNodes,relations:[edge('college-group','college','directory'),edge('college','college-notices')]});
assert.equal(college.actual.get('college-notices').parent,college.actual.get('college'),'Projected directory membership must connect to the actual unit sphere');
assert.equal(college.actual.get('college').parent,college.actual.get('college-group'),'The unit must connect to its actual directory sphere');
const many = Array.from({length:1200}, (_, index) => node('p'+index,'页面'+index,['学校官网','组织机构','院系'+(index%40)]));
assert.equal(buildAtlas({school,nodes:many}).actual.size, 1200, 'No hidden truncation of published entries');
const targetA={...node('a','同名入口',[]),state:'running',tasks:[{state:'running',updated_at:'2026-10-18T01:00:00Z'}]};
const targetB={...node('b','同名入口',[]),state:'connected',tasks:[{state:'running',updated_at:'2026-10-18T02:00:00Z'}]};
assert.equal(trackingTarget({nodes:[targetA,targetB]}).node.id,'b','Follow newest real work, including work attached to a connected node');
assert.equal(trackingTarget({nodes:[{...targetB,tasks:[]}],tasks:[{url:targetB.url,state:'done',updated_at:'2026-10-18T03:00:00Z'}]}).node.id,'b','A completed latest step is still a real tracking target');
assert.equal(trackingTarget({nodes:records,tasks:[]}),null,'Edges and matching labels alone are not proof of current exploration');
const units=Array.from({length:40},(_,index)=>node('unit'+index,'院系'+index,[],'unit'));
const denseNodes=[node('root','学校官网',[],'root'),node('directory','院系设置',[],'directory'),...units,...many];
const denseLinks=[edge('root','directory','directory'),...units.map(n=>edge('directory',n.id,'directory')),...many.map((n,i)=>edge('unit'+i%40,n.id))];
const clustered=buildAtlas({school,nodes:denseNodes,relations:denseLinks});
function viewAreas(atlas) {
    const points=[...atlas.actual.values()].map(n=>n.position);
    const normalize=v=>{const n=Math.hypot(...v);return v.map(x=>x/n);};
    const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
    const dot=(p,v)=>p.x*v[0]+p.y*v[1]+p.z*v[2];
    return [[0,0,1],[1,0,0],[0,1,0],[1,1,1],[1,-1,1],[1,0,1]].map(view=>{
        const normal=normalize(view),right=normalize(cross(normal,Math.abs(normal[1])>.9?[1,0,0]:[0,1,0])),up=cross(right,normal);
        const projected=points.map(p=>[dot(p,right),dot(p,up)]).sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
        const turn=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
        const half=values=>{const result=[];for(const point of values){while(result.length>1&&turn(result.at(-2),result.at(-1),point)<=0)result.pop();result.push(point);}return result;};
        const hull=[...half(projected).slice(0,-1),...half([...projected].reverse()).slice(0,-1)];
        return Math.abs(hull.reduce((area,p,i)=>{const q=hull[(i+1)%hull.length];return area+p[0]*q[1]-p[1]*q[0];},0))/2;
    });
}
const areas=viewAreas(clustered), areaRatio=Math.min(...areas)/Math.max(...areas);
assert.ok(areaRatio>.7,'Equidistant front, side, top and diagonal projections remain comparably large: '+JSON.stringify({areaRatio,areas}));
function covarianceRatio(atlas){
    const positions=[...atlas.actual.values()].map(n=>[n.position.x,n.position.y,n.position.z]);
    const mean=[0,1,2].map(axis=>positions.reduce((sum,p)=>sum+p[axis],0)/positions.length);
    const matrix=[0,1,2].map(a=>[0,1,2].map(b=>positions.reduce((sum,p)=>sum+(p[a]-mean[a])*(p[b]-mean[b]),0)/positions.length));
    for(let pass=0;pass<18;pass++){
        const [p,q]=[[0,1],[0,2],[1,2]].sort((a,b)=>Math.abs(matrix[b[0]][b[1]])-Math.abs(matrix[a[0]][a[1]]))[0];
        if(Math.abs(matrix[p][q])<1e-9)break;
        const angle=.5*Math.atan2(2*matrix[p][q],matrix[q][q]-matrix[p][p]),c=Math.cos(angle),s=Math.sin(angle);
        const pp=matrix[p][p],qq=matrix[q][q],pq=matrix[p][q];
        matrix[p][p]=c*c*pp-2*s*c*pq+s*s*qq;matrix[q][q]=s*s*pp+2*s*c*pq+c*c*qq;matrix[p][q]=matrix[q][p]=0;
        for(let r=0;r<3;r++)if(r!==p&&r!==q){const rp=matrix[r][p],rq=matrix[r][q];matrix[r][p]=matrix[p][r]=c*rp-s*rq;matrix[r][q]=matrix[q][r]=s*rp+c*rq;}
    }
    const variances=[0,1,2].map(i=>matrix[i][i]);return Math.min(...variances)/Math.max(...variances);
}
const isotropy=covarianceRatio(clustered);
assert.ok(isotropy>.55,'The weakest principal direction retains real spatial spread, including between the sampled views: '+isotropy);
assert.ok(new Set([...clustered.entries.values()].map(n=>n.cluster)).size>=40,'Each recorded unit branch has its own layout group');
const center=clustered.actual.get('unit0').position;
const unitPeers=[...clustered.actual.values()].filter(n=>n.cluster==='unit0'&&n.id!=='unit0');
const localExtent=Math.max(...unitPeers.map(n=>Math.hypot(n.position.x-center.x,n.position.y-center.y)));
const viewExtent=Math.max(...[...clustered.actual.values()].map(n=>Math.hypot(n.position.x,n.position.y)));
assert.ok(localExtent<viewExtent*.3,'Columns from one unit remain a compact local group within the overview');
const xs=[...clustered.actual.values()].map(n=>n.position.x),ys=[...clustered.actual.values()].map(n=>n.position.y);
const aspect=(Math.max(...xs)-Math.min(...xs))/(Math.max(...ys)-Math.min(...ys));
assert.ok(aspect>.55&&aspect<1.8,'Default projection uses both screen dimensions instead of a thin tower');
const spread=buildAtlas({school,nodes:denseNodes,relations:denseLinks},{spacing:1.8});
const envelope=m=>Math.max(...[...m.entries.values()].map(n=>Math.hypot(n.position.x,n.position.y,n.position.z)));
assert.ok(envelope(spread)>envelope(clustered),'Branch spacing is a working layout control');
console.log('Discovery atlas model: identity, truthful paths, filtering, folding, spatial layout and 1,200-entry checks passed. Projection area ratio: '+areaRatio.toFixed(3)+'; covariance ratio: '+isotropy.toFixed(3)+'.');
