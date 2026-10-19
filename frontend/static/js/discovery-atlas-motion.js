/* Presentation of saved paths only: these frames never create discovery evidence. */
export const clamp = value => Math.min(1, Math.max(0, value));
export const smooth = value => { const t=clamp(value); return t*t*(3-2*t); };
export const identity = item => item.record ? 'page:'+item.record.id : item.id;
export const mix = (a,b,t) => ({x:a.x+(b.x-a.x)*t,y:a.y+(b.y-a.y)*t,z:a.z+(b.z-a.z)*t});

export function birthAnchor(item, previous) {
    const parent=previous.get(identity(item.parent || item));
    const child=item.children.map(node=>previous.get(identity(node))).find(Boolean);
    if (parent && child) return mix(parent.position,child.position,.5);
    return parent?.position || child?.position || item.parent?.position || item.position;
}

export function growthPlan(nodes, {parents = null} = {}) {
    const plan=new Map(), shown=new Set(nodes.map(node=>node.id));
    const parentOf=item=>parents?.has(item.id)?parents.get(item.id):item.parent;
    function add(item) {
        if (plan.has(item.id)) return plan.get(item.id);
        const upstream=parentOf(item);
        const parent=upstream && shown.has(upstream.id) ? add(upstream) : null;
        const siblings=parents?nodes.filter(node=>parentOf(node)===upstream):item.parent?.children || nodes.filter(node=>!node.parent);
        const index=Math.max(0,siblings.indexOf(item));
        const start=parent ? parent.start+.38 + .28*index/Math.max(1,siblings.length-1) : 0;
        const value={start,end:start+.85}; plan.set(item.id,value); return value;
    }
    nodes.forEach(add);
    const end=Math.max(1,...[...plan.values()].map(item=>item.end));
    return {entries:plan,duration:end};
}

export function growthAt(plan,item,progress) {
    const phase=plan.entries.get(item.id);
    return phase ? smooth((clamp(progress)*plan.duration-phase.start)/(phase.end-phase.start)) : 1;
}
