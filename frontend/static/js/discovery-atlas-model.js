/* Geometry follows recorded navigation names, never guessed administrative links. */
export function buildAtlas(snapshot) {
    const entries = new Map(), actual = new Map();
    const cleanUrl = value => (value || '').replace(/\/$/, '');
    const roots = snapshot.nodes.filter(node => node.kind === 'root' || cleanUrl(node.url) === cleanUrl(snapshot.school.url));
    const rootNames = new Set(['学校官网', snapshot.school.name, ...roots.map(node => node.label)]);
    const ensure = (parts, orphan = false) => {
        const key = JSON.stringify(parts);
        if (entries.has(key)) return entries.get(key);
        const parent = parts.length > 1 && !orphan ? ensure(parts.slice(0, -1)) : null;
        const item = {id:key, label:parts.at(-1) === '@school' ? snapshot.school.name : parts.at(-1),
            parent, children:[], records:[], record:null, depth:parent ? parent.depth + 1 : 0, orphan};
        entries.set(key, item);
        if (parent) parent.children.push(item);
        return item;
    };
    for (const record of snapshot.nodes) {
        let parts = [...record.path];
        if (parts.at(-1) === record.label) parts.pop();
        if (parts.length && rootNames.has(parts[0])) parts[0] = '@school';
        const isRoot = roots.includes(record);
        const orphan = !parts.length && !isRoot;
        parts = isRoot ? ['@school'] : orphan ? ['@unplaced:' + record.id] : [...parts, record.label];
        const item = ensure(parts, orphan);
        if (orphan) item.label = record.label;
        item.records.push(record);
    }
    // Identical names on different URLs are separate pages, not a merged identity.
    for (const item of [...entries.values()]) {
        if (item.records.length === 1) {
            item.record = item.records[0]; actual.set(item.record.id, item);
        } else for (const record of item.records) {
            const child = {id:item.id + ':' + record.id, label:record.label, parent:item, children:[],
                records:[record], record, depth:item.depth + 1, orphan:item.orphan};
            item.children.push(child); entries.set(child.id, child); actual.set(record.id, child);
        }
    }
    const hash = text => [...text].reduce((value, char) => (Math.imul(value, 31) + char.charCodeAt(0)) >>> 0, 7);
    for (const item of entries.values()) item.children.sort((a, b) => a.id.localeCompare(b.id));
    const forest = [...entries.values()].filter(item => !item.parent).sort((a,b) => a.id.localeCompare(b.id));
    function place(item, position, index = 0) {
        item.position = position;
        const kind = item.record?.kind;
        item.radius = kind === 'root' || item.id === '["@school"]' ? 14 : kind === 'unit' ? 9 : kind === 'directory' ? 10 :
            kind === 'channel' ? 4.3 : item.children.length ? 6 : 2.9;
        const lineage = ancestors(item);
        const branch = [...lineage].reverse().find(part => part.record?.kind === 'unit') || lineage[Math.min(2, lineage.length - 1)];
        item.branch = hash(branch.id) % 5;
        item.children.forEach((child, childIndex) => {
            const count = item.children.length;
            const angle = childIndex * 2.399963 + hash(item.id) % 19;
            let next;
            if (item.depth === 0) {
                next = {x:position.x + 200, y:position.y + (childIndex - (count - 1) / 2) * Math.min(90, 210 / Math.max(1, count - 1)),
                    z:position.z + Math.sin(angle) * (count > 1 ? 45 : 15)};
            } else if (item.depth === 1) {
                next = {x:position.x + 180 + Math.cos(angle) * 35,
                    y:position.y + (childIndex - (count - 1) / 2) * Math.min(65, 220 / Math.max(1, count - 1)),
                    z:position.z + Math.sin(angle) * 65};
            } else {
                const reach = 20 + 7 * Math.sqrt(count);
                next = {x:position.x + 65 + (Math.cos(angle) + 1) * 26,
                    y:position.y + Math.cos(angle) * reach * (.55 + (childIndex % 3) * .2),
                    z:position.z + Math.sin(angle) * reach};
            }
            place(child, next, index + childIndex);
        });
    }
    const knownRoots = forest.filter(item => !item.orphan), unplaced = forest.filter(item => item.orphan);
    forest.forEach(item => {
        const index = item.orphan ? unplaced.indexOf(item) : knownRoots.indexOf(item);
        place(item, {x:-280 + (item.orphan ? index % 5 * 44 : 0),
        y:item.orphan ? -210 - Math.floor(index / 5) * 25 : (index - (knownRoots.length - 1) / 2) * 120 - 40,
        z:item.orphan ? -40 : 0});
    });
    return {entries, actual, forest};
}

export function ancestors(item) {
    const path = [];
    for (let cursor = item; cursor; cursor = cursor.parent) path.unshift(cursor);
    return path;
}

export function visibleEntries(model, {query = '', state = 'all', collapsed = new Set()} = {}) {
    const matching = new Set();
    const filtering = Boolean(query) || state !== 'all';
    if (filtering) for (const item of model.entries.values()) {
        const record = item.record;
        if (record && (state === 'all' || record.state === state) &&
            (!query || [record.label, ...record.path].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase()))) {
            ancestors(item).forEach(part => matching.add(part.id));
        }
    }
    return [...model.entries.values()].filter(item => {
        if (filtering) return matching.has(item.id);
        return !ancestors(item).slice(0, -1).some(part => collapsed.has(part.id));
    });
}
