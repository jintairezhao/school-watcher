import assert from 'node:assert/strict';
import {placeLabelBox,labelIntersectsSphere} from '../../frontend/static/js/discovery-atlas-view.js';

const options={x:195,y:400,offset:24,width:80,height:24,viewportWidth:390,viewportHeight:800,
    occupied:[[219,388,299,412],[91,388,171,412],[155,352,235,376],[155,424,235,448]],
    sphereObstacles:[{x:195,y:400,radius:16}]};
assert.equal(placeLabelBox(options),null,'Unfocused labels keep their normal decluttering policy');
const before=JSON.stringify(options);
const box=placeLabelBox({...options,focused:true});
assert.ok(box,'A selected name must try available diagonal space after its four adjacent slots are blocked');
assert.ok(box[0]>=4&&box[1]>=4&&box[2]<=386&&box[3]<=796);
assert.ok(!options.occupied.some(other=>box[0]<other[2]&&box[2]>other[0]&&box[1]<other[3]&&box[3]>other[1]));
assert.ok(!options.sphereObstacles.some(sphere=>labelIntersectsSphere(box,sphere)));
assert.equal(JSON.stringify(options),before,'Placement cannot change shared obstacles');
assert.equal(placeLabelBox({...options,focused:true,occupied:[[0,0,390,800]]}),null,'Controls stay protected even if no safe label position exists');
assert.equal(placeLabelBox({...options,focused:true,viewportWidth:70}),null,'The name is not shrunk or clipped to force a placement');
const ordinary={x:100,y:100,offset:15,width:60,height:20,viewportWidth:390,viewportHeight:800};
assert.deepEqual(placeLabelBox(ordinary),[115,90,175,110]);
assert.deepEqual(placeLabelBox({...ordinary,focused:true}),placeLabelBox(ordinary),'Keep the usual slot when it fits');
console.log('Focused label placement preserves bounds, controls, sphere hit areas and normal decluttering');
