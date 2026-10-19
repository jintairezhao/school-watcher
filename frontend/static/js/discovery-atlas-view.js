/* Display policy only. Hidden labels and cross-links remain in the evidenced model. */
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

export function labelImportance(item, {selected, hovered, path, context, detailed, pixels, nodeLabels = true}) {
    if (!nodeLabels) return -1;
    if (item === selected) return 1e6;
    if (item === hovered) return 9e5;
    if (path.has(item.id)) return 8e5 - item.depth;
    if (selected || hovered) return context.has(item.id) ? 5000 + pixels : -1;
    const topDirectory=item.record.kind==='directory' && item.parent?.record.kind==='root';
    const rank = topDirectory ? 4.5 : {root: 5, unit: 4, directory: 3}[item.record.kind] || 0;
    if (!rank && !detailed && pixels < 7) return -1;
    return rank * 100 + Math.min(pixels, 30) + Math.min(item.degree || 0, 20);
}
