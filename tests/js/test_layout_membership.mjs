import assert from 'node:assert/strict';
import * as layout from '../../frontend/static/js/structure-layout.js';

// Synthetic guards for semantics only, not real website/branch evidence.
const evidence=[{reference_url:'https://fixture.example/directory',locator:'main a',content_hash:'fixture-material'}];
const relation=(id,source,target,type,observed_relation)=>({id,source,target,type,observed_relation,evidence});
const nodes=[{id:'directory',entity_type:'directory_group',label:'院系目录'},
    {id:'organization',entity_type:'organization',label:'示例学院'},
    {id:'source',entity_type:'source',configured:true},{id:'page',entity_type:'page'}];
const relations=[relation('listed-organization','directory','organization','directory_listing','directory_entry'),
    relation('listed-source','organization','source','directory_listing','observed_website_entrance'),
    relation('hosted','page','source','source_on_page')];
const input={nodes,relations};
const output=layout.layoutStructure(input);
assert.equal(output.constraints.groups.length,0,'Directory inclusion must not silently create ownership groups');
assert.equal(output.sizes.organization.connectedSourceCount,0,'An entrance association does not establish the publisher');
assert.equal(output.sizes.directory.connectedSourceCount,0,'A roster is not an organization owning its entries');
assert.equal(output.constraints.relations,3,'Valid directory relationships still influence geometry');

const groups=layout.directoryDisplayGroups(input);
assert.equal(groups.length,1,'Only actual directory entities form automatic directory display suggestions');
assert.equal(groups[0].kind,'display');
assert.equal(groups[0].establishesOwnership,false);
assert.deepEqual(groups[0].relationIds,['listed-organization']);
assert.deepEqual(groups[0].members,['directory','organization']);
assert.ok(!groups[0].members.includes('source'),'Display suggestions cannot traverse an organization into inferred publishers');
const explicit=layout.layoutStructure({...input,groups});
assert.equal(explicit.constraints.groups[0].origin,'explicit_display_group');
assert.equal(explicit.sizes.organization.connectedSourceCount,0);

const owned=layout.layoutStructure({...input,groups,relations:[...relations,
    relation('owns','organization','source','source_ownership','verified_publisher')]});
assert.equal(owned.sizes.organization.connectedSourceCount,1);
assert.equal(owned.constraints.groups.filter(group=>group.origin==='evidenced_membership').length,1);
assert.equal(owned.constraints.groups.filter(group=>group.origin==='explicit_display_group').length,1);
assert.equal(owned.constraints.groups.find(group=>group.origin==='evidenced_membership').relationType,'source_ownership');
assert.deepEqual(owned.constraints.groups.find(group=>group.origin==='evidenced_membership').relationIds,['owns']);

const organizations=[{id:'parent',entity_type:'organization'},{id:'child',entity_type:'organization'}];
const differentPredicates=layout.layoutStructure({nodes:organizations,relations:[
    relation('attached','parent','child','organization_membership','attached_unit'),
    relation('subordinate','parent','child','organization_membership','subordinate_unit')]});
assert.equal(differentPredicates.constraints.relations,2,'Different factual predicates retain their own participation');
assert.equal(differentPredicates.constraints.groups.length,2);
assert.deepEqual(differentPredicates.constraints.groups.map(group=>group.predicate).sort(),['attached_unit','subordinate_unit']);
console.log('Layout memberships: directories remain display-only, publisher counts require ownership, distinct factual predicates retained.');
