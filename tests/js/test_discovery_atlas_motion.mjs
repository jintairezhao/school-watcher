import assert from 'node:assert/strict';
import {birthAnchor,growthPlan,growthAt,identity} from '../../frontend/static/js/discovery-atlas-motion.js';
import {buildAtlas} from '../../frontend/static/js/discovery-atlas-model.js';

const root={id:'a',record:{id:'a'},position:{x:0,y:0,z:0},children:[]};
const child={id:'b',record:{id:'b'},parent:root,position:{x:10,y:20,z:30},children:[]};
root.children=[child];
const plan=growthPlan([child,root]);
assert.ok(plan.entries.get('b').start>plan.entries.get('a').start,'Parents begin growing before descendants, independent of input order');
const values=Array.from({length:241},(_,frame)=>growthAt(plan,child,frame/240));
assert.equal(values[0],0);assert.equal(values.at(-1),1);
assert.ok(new Set(values.filter(value=>value>0&&value<1)).size>100,'Growth has many intermediate frames rather than four chapter cuts');
assert.ok(values.every((v,i)=>!i||v>=values[i-1]));
assert.ok(Math.max(...values.slice(1).map((v,i)=>v-values[i]))<.02,'Frame-to-frame growth remains smooth');
const retained={id:'c',record:{id:'c'},position:{x:20,y:30,z:40},children:[],parent:child};child.children=[retained];
const previous=new Map([[identity(root),{position:root.position}],[identity(retained),{position:retained.position}]]);
assert.deepEqual(birthAnchor(child,previous),{x:10,y:15,z:20},'An inserted node emerges between the two retained endpoints');
assert.equal(identity({...child,id:'new-path-id'}),identity(child),'Animation identity survives a recorded path change');
const alternative={id:'alternative',parent:null,children:[],position:{x:0,y:50,z:0}},page={...child,id:'page',parent:alternative};
const selectedPlan=growthPlan([root,child,page],{parents:new Map([['a',null],['b',root],['page',child]])});
assert.ok(selectedPlan.entries.get('page').start>selectedPlan.entries.get('b').start,'Selected-path growth follows evidenced route order rather than a different layout parent');

const records=[{id:'root',url:'https://example.edu',label:'Home',path:[],kind:'root',state:'fetched'}],relations=[];
for(let unit=0;unit<18;unit++) {
    records.push({id:`unit-${unit}`,url:`https://example.edu/unit/${unit}`,label:`Unit ${unit}`,path:[],kind:'unit'});
    relations.push({source:'root',target:`unit-${unit}`,type:'navigation',evidence_url:'https://example.edu'});
    for(let leaf=0;leaf<5;leaf++){
        records.push({id:`${unit}-${leaf}`,url:`https://example.edu/unit/${unit}/${leaf}`,label:`Channel ${leaf}`,kind:'channel',state:'fetched',path:['Home','Departments',`Unit ${unit}`]});
        relations.push({source:`unit-${unit}`,target:`${unit}-${leaf}`,type:'navigation',evidence_url:`https://example.edu/unit/${unit}`});
    }
}
const atlas=buildAtlas({school:{name:'Example University'},nodes:records,relations});
const points=[...atlas.entries.values()].map(n=>n.position), mean=key=>points.reduce((sum,p)=>sum+p[key],0)/points.length;
const axes=['x','y','z'], means=axes.map(mean);
const covariance=axes.map((a,i)=>axes.map((b,j)=>points.reduce((sum,p)=>sum+(p[a]-means[i])*(p[b]-means[j]),0)/points.length));
const [a,b,c]=covariance;
const determinant=a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]);
const trace=a[0]+b[1]+c[2];
assert.ok(determinant/(trace**3)>.005,'Branching data occupies a volume, not a plane with tiny depth offsets');
console.log('Atlas motion: continuous growth, parent order, inserted bubbles, stable identities and volumetric layout passed.');
