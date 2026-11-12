import assert from 'node:assert/strict';
import {structureGraphDistances} from '../../frontend/static/js/structure-layout.js';

// Independent all-pairs reference for small algorithm contracts, not website
// coverage or performance evidence. Actual retained graphs are measured apart.
function reference(adjacency){
    const n=adjacency.length,rows=Array.from({length:n},(_,i)=>Array.from({length:n},(_,j)=>i===j?0:Infinity));
    adjacency.forEach((edges,i)=>edges.forEach(edge=>rows[i][edge.to]=Math.min(rows[i][edge.to],edge.length)));
    for(let k=0;k<n;k++)for(let i=0;i<n;i++)for(let j=0;j<n;j++)rows[i][j]=Math.min(rows[i][j],rows[i][k]+rows[k][j]);
    return rows;
}
const fixtures=[
    [[]],
    [[{to:1,length:8},{to:1,length:2},{to:2,length:10}],[{to:2,length:3}],[]],
    [[{to:1,length:0},{to:2,length:4}],[{to:0,length:0},{to:2,length:4}],[{to:3,length:1}],[],[]],
    Array.from({length:12},(_,i)=>Array.from({length:12},(_,j)=>({to:j,length:(i*3+j*5)%11+1})).filter(edge=>edge.to!==i)),
];
for(const adjacency of fixtures){
    const before=JSON.stringify(adjacency),expected=reference(adjacency);
    for(let origin=0;origin<adjacency.length;origin++){
        assert.deepEqual(structureGraphDistances(adjacency,origin),expected[origin]);
        assert.deepEqual(structureGraphDistances(adjacency.map(edges=>[...edges].reverse()),origin),expected[origin]);
    }
    assert.equal(JSON.stringify(adjacency),before);
}
assert.throws(()=>structureGraphDistances([],0),/Invalid distance origin/);
assert.throws(()=>structureGraphDistances([[]],.5),/Invalid distance origin/);
for(const edge of [{to:1,length:1},{to:0,length:-1},{to:0,length:NaN},{to:0,length:Infinity}])
    assert.throws(()=>structureGraphDistances([[edge]],0),/Invalid geometry distance edge/);
console.log('Geometry shortest distances match independent all-pairs results across directed, cyclic, tied and disconnected fixtures.');
