/* Pick the currently drawn spheres, without stale InstancedMesh bounds. */
export function pickSphere(nodes, {origin, direction, near = 0, far = Infinity, pixelFactor = 0, minimumPixels = 4}) {
    function nearest(expanded) {
        let chosen = null, closest = far;
        for (const node of nodes) {
            if ((node.growth ?? 1) < .15) continue;
            const point = node.renderPosition || node.position;
            const x=point.x-origin.x,y=point.y-origin.y,z=point.z-origin.z;
            const distanceSquared=x*x+y*y+z*z, along=x*direction.x+y*direction.y+z*direction.z;
            const drawn=node.drawRadius ?? node.radius;
            const radius=expanded ? Math.max(drawn,Math.sqrt(distanceSquared)*pixelFactor*minimumPixels) : drawn;
            const perpendicular=Math.max(0,distanceSquared-along*along);
            if (!(radius>0) || perpendicular>radius*radius) continue;
            const half=Math.sqrt(radius*radius-perpendicular);
            const entry=along-half, exit=along+half, hit=entry>=near?entry:exit;
            if (hit<near || hit>=closest) continue;
            closest=hit;chosen=node;
        }
        return chosen;
    }
    // A larger target helps tiny spheres, but cannot steal an actual surface hit.
    return nearest(false) || nearest(true);
}
