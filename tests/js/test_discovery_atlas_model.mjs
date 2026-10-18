import assert from 'node:assert/strict';
import {buildAtlas, ancestors, visibleEntries} from '../../frontend/static/js/discovery-atlas-model.js';

const node = (id, label, path, kind = 'channel', state = 'fetched') =>
    ({id, label, path, kind, state, url:'https://example.edu.cn/' + id});
const school = {name:'演示大学', url:'https://example.edu.cn'};
const records = [
    node('orphan','路径未知栏目',[]),
    node('root','学校官网',[],'root'),
    node('unit','研究生院',['学校官网','组织机构'],'unit'),
    node('channel-a','通知公告',['学校官网','组织机构','研究生院'],'channel','connected'),
    node('channel-b','通知公告',['学校官网','组织机构','研究生院'],'channel','failed'),
    node('other','通知公告',['学校官网','院系设置','外国语学院'],'channel','pending')
];
const model = buildAtlas({school, nodes:records});
assert.equal(model.actual.size, records.length, 'Same-named pages on different URLs must remain distinct');
assert.equal(model.actual.get('orphan').parent, null, 'Unknown paths must not acquire a school-root edge');
assert.deepEqual(ancestors(model.actual.get('unit')).map(item => item.label), ['演示大学','组织机构','研究生院']);
assert.equal(model.actual.get('channel-a').parent, model.actual.get('channel-b').parent);
assert.notEqual(model.actual.get('channel-a'), model.actual.get('channel-b'));
assert.notEqual(model.actual.get('channel-a').parent, model.actual.get('other').parent);
assert.ok([...model.entries.values()].filter(item => !item.record).every(item => !item.url), 'Named path groups are not fetched pages');
assert.ok(model.actual.get('root').radius > model.actual.get('unit').radius);
assert.ok(model.actual.get('unit').radius > model.actual.get('channel-a').radius);
assert.ok(new Set([...model.entries.values()].map(item => item.position.z)).size > 2, 'Layout must occupy genuine spatial depth');
const collapsed = new Set([model.actual.get('unit').id]);
assert.ok(!visibleEntries(model, {collapsed}).includes(model.actual.get('channel-a')));
assert.ok(visibleEntries(model, {state:'failed',collapsed}).includes(model.actual.get('channel-b')), 'Filtering reveals matching descendants with their ancestors');
assert.deepEqual(visibleEntries(model, {query:'路径未知'}).map(item => item.record?.id).filter(Boolean), ['orphan']);
assert.equal(visibleEntries(model, {query:'missing'}).length, 0);
const reversed = buildAtlas({school,nodes:[...records].reverse()});
assert.deepEqual(model.actual.get('root').position, reversed.actual.get('root').position, 'Unplaced pages must not shift the school root');
assert.equal(buildAtlas({school,nodes:[]}).entries.size, 0);
const many = Array.from({length:1200}, (_, index) => node('p'+index,'页面'+index,['学校官网','组织机构','院系'+(index%40)]));
assert.equal(buildAtlas({school,nodes:many}).actual.size, 1200, 'No hidden truncation of published entries');
console.log('Discovery atlas model: identity, truthful paths, filtering, folding, spatial layout and 1,200-entry checks passed.');
