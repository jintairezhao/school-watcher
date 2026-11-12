import assert from 'node:assert/strict';
import {relaxSphericalVolume} from '../../frontend/static/js/structure-layout.js';

// A geometric penalty should not change its meaning when the same graph is
// rotated. This fixture certifies no real-world layout or source relationships.
const points=[{x:4,y:0,z:0},{x:-4,y:0,z:0},{x:0,y:2,z:0},{x:0,y:-2,z:0},
    {x:0,y:0,z:.1},{x:0,y:0,z:-.1}];
const rotate=p=>({x:(p.x+p.z)/Math.SQRT2,y:p.y,z:(p.z-p.x)/Math.SQRT2});
const straight=structuredClone(points),rotated=points.map(rotate),mobility=points.map(()=>1);
relaxSphericalVolume(straight,mobility);relaxSphericalVolume(rotated,mobility);
const expected=straight.map(rotate);
assert.ok(rotated.every((p,index)=>['x','y','z'].every(axis=>Math.abs(p[axis]-expected[index][axis])<1e-9)),
    'The same 3D shape must receive the same weak spherical correction in every orientation');
assert.ok(straight[4].z>points[4].z,'The thin direction receives a small expansion');
assert.ok(Math.hypot(straight[0].x-points[0].x,straight[0].y,straight[0].z)<.05,
    'The spherical preference is bounded, not a hard relocation');
console.log('Spherical constraint: rotation-invariant covariance and bounded correction passed.');
