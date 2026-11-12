/* Display policy only. Hidden labels and cross-links remain in the evidenced model. */
import {baseLabelLevel,labelHierarchyLevels,labelAncestors} from './discovery-atlas-label-hierarchy.js';
export {labelHierarchyLevels,labelAncestors};
export function focusContext(links, selected, hovered, path = []) {
    const ids = new Set(path.map(node => node.id));
    for (const node of [selected, hovered]) {
        if (!node) continue;
        ids.add(node.id);
        for (const edge of links) {
            if (edge.from === node) ids.add(edge.to.id);
            if (edge.to === node) ids.add(edge.from.id);
        }
    }
    for(const id of labelAncestors(links,selected,hovered))ids.add(id);
    return ids;
}

export function displayLink(edge, selected, hovered) {
    // One evidenced spanning forest in the overview. Cross-links are available
    // when their endpoint is inspected, rather than all competing at once.
    return Boolean(edge.primary || edge.to.parentRelation === edge ||
        (selected && (edge.from === selected || edge.to === selected)) ||
        (hovered && (edge.from === hovered || edge.to === hovered)));
}

export function labelPolicy({width, height, zoom, selected}) {
    const narrow = width < 600;
    return {limit: Math.max(narrow ? 5 : 10, Math.min(narrow ? 12 : 42,
        Math.floor(width * height / (zoom > 1.65 || selected ? 23500 : 44000)))),
        detailed: zoom > 1.65};
}

export function labelIntersectsSphere(box,sphere) {
    const x=Math.max(box[0],Math.min(sphere.x,box[2])),y=Math.max(box[1],Math.min(sphere.y,box[3]));
    return (x-sphere.x)**2+(y-sphere.y)**2<sphere.radius**2;
}

export function placeLabelBox({x,y,offset,width,height,viewportWidth,viewportHeight,
    occupied=[],sphereObstacles=[],focused=false,searchResult=false}) {
    const positions=[[x+offset,y-height/2],[x-offset-width,y-height/2],
        [x-width/2,y-offset-height],[x-width/2,y+offset]];
    if(focused)for(const extra of (searchResult?[0,16,32,64,96,128]:[0,16,32,64])){
        const distance=offset+extra;
        positions.push([x+distance,y-distance-height],[x-distance-width,y-distance-height],
            [x+distance,y+distance],[x-distance-width,y+distance]);
        if(extra)positions.push([x+distance,y-height/2],[x-distance-width,y-height/2],
            [x-width/2,y-distance-height],[x-width/2,y+distance]);
    }
    for(const [left,top] of positions){
        const box=[left,top,left+width,top+height];
        if(left<4||top<4||box[2]>viewportWidth-4||box[3]>viewportHeight-4)continue;
        if(occupied.some(other=>left<other[2]&&box[2]>other[0]&&top<other[3]&&box[3]>other[1]))continue;
        if(sphereObstacles.some(sphere=>labelIntersectsSphere(box,sphere)))continue;
        return box;
    }
    return null;
}

export function labelSearchMatch(item,query) {
    const text=String(query||'').trim().toLocaleLowerCase();
    return Boolean(text&&String(item.label||item.record?.label||'').toLocaleLowerCase().includes(text));
}
export function searchLabelLevel(items,levels,query) {
    const matched=items.filter(item=>labelSearchMatch(item,query));
    return matched.length?Math.min(...matched.map(item=>levels.get(item.id)??baseLabelLevel(item))):null;
}

export function labelImportance(item, {selected, hovered, path, context, detailed, pixels, nodeLabels = true,
    hierarchyLevel=baseLabelLevel(item),upstream=new Set(),query='',searchLevel=null}) {
    if (!nodeLabels) return -1;
    if (item === selected) return 1e6;
    if (item === hovered) return 9e5;
    const level=Math.max(0,Math.min(100,hierarchyLevel));
    const detail=Math.min(pixels,30)+Math.min(item.degree||0,20);
    const searching=Boolean(String(query).trim());
    // Filtering keeps navigation context, but its labels must not displace the
    // names being searched for. Explicit matches also remain readable at any
    // sphere size, and while an earlier, unrelated node is still selected.
    if(labelSearchMatch(item,query)){
        if(!detailed&&searchLevel!==null&&level>searchLevel)return -1;
        return 7e5-level*100+detail;
    }
    if(searching&&(item.record.path||[]).some(part=>String(part).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())))
        return 6e5-level*100+detail;
    if (path.has(item.id)||upstream.has(item.id)) return (searching?5e5:8e5) - level*100 + detail;
    if (selected || hovered) return context.has(item.id) ? 5e4 - level*100 + detail : -1;
    const topDirectory=item.record.kind==='directory' && item.parent?.record.kind==='root';
    const rank = topDirectory ? 4.5 : {root: 5, unit: 4, directory: 3}[item.record.kind] || (item.relationSummaryCount?2.5:0);
    if (!rank && !detailed && pixels < 7) return -1;
    return 5e4 - level*100 + detail;
}
