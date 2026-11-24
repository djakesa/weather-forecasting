"""Connected field junctions optimized within PV polygons, no fixed centroids."""
from shapely.geometry import Point
from shapely.ops import nearest_points
from backend.domain.costs import connection
from backend.domain.geometry import metric
from shapely.geometry import mapping, shape


def split_fields(features):
    result=[]
    for feature in features:
        geometry=shape(feature["geometry"])
        parts=list(geometry.geoms) if geometry.geom_type=="MultiPolygon" else [geometry]
        areas=[metric(p).area for p in parts]
        total=sum(areas)
        for index,(part,area) in enumerate(zip(parts,areas)):
            props=dict(feature["properties"])
            reference=props.get("idu",props.get("id","—"))
            props["champ_id"]=f"{reference}/champ-{index+1}" if len(parts)>1 else reference
            for key in ("pv_m2","emprise_m2"):
                props[key]=props[key]*area/total
            result.append(dict(type="Feature",geometry=mapping(part),properties=props))
    return result


def tree(shapes,root):
    distances={i:(shapes[root].distance(shapes[i]),root) for i in range(len(shapes)) if i!=root}
    edges=[]
    while distances:
        child=min(distances,key=lambda i:(distances[i][0],i))
        _,parent=distances.pop(child)
        edges.append((child,parent))
        for other in distances:
            distance=shapes[child].distance(shapes[other])
            if distance<distances[other][0]:
                distances[other]=distance,child
    return edges


def optimized_junctions(parcels,assets):
    shapes=[p.get("footprint",p["geometry"]) for p in parcels]
    original=[p["geometry"] for p in parcels]
    choices=[(i,connection(s,assets)) for i,s in enumerate(shapes)]
    choices=[(i,c) for i,c in choices if c is not None]
    root,target=min(choices,key=lambda v:v[1]["distance_m"]) if choices else (0,None)
    external=target["geometry"] if target else None
    def optimize(edges):
        points=list(original)
        neighbors={i:[] for i in range(len(points))}
        for a,b in edges:
            neighbors[a].append(b)
            neighbors[b].append(a)
        for _ in range(24):
            improvement=0
            for i,region in enumerate(shapes):
                anchors=[points[j] for j in neighbors[i]]
                if i==root and external is not None:
                    anchors.append(nearest_points(points[i],external)[1])
                if not anchors:
                    continue
                candidates=[points[i]]+[nearest_points(region,a)[0] for a in anchors]
                median=Point(sum(a.x for a in anchors)/len(anchors),sum(a.y for a in anchors)/len(anchors))
                for _ in range(15):
                    weights=[1/max(median.distance(a),.001) for a in anchors]
                    median=Point(sum(a.x*w for a,w in zip(anchors,weights))/sum(weights),sum(a.y*w for a,w in zip(anchors,weights))/sum(weights))
                candidates.append(nearest_points(region,median)[0])
                def cost(p):
                    value=sum(p.distance(points[j]) for j in neighbors[i])
                    return value+p.distance(external) if i==root and external is not None else value
                best=min(candidates,key=cost)
                improvement += cost(points[i])-cost(best)
                points[i]=best
            if improvement<.01:
                break
        length=sum(points[a].distance(points[b]) for a,b in edges)
        if external is not None:
            length+=points[root].distance(external)
        return length,points,edges
    # Compare the polygon-distance tree against the former point-distance tree.
    _,points,edges=min([optimize(tree(shapes,root)), optimize(tree(original,root))],key=lambda c:c[0])
    if target:
        target={**target,"target":nearest_points(points[root],external)[1],"distance_m":points[root].distance(external)}
    return points,[(a,b,points[a].distance(points[b])) for a,b in edges],root,target
