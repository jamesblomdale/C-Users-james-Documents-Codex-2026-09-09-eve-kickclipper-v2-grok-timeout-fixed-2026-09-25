"""Optional competitor-clip oracle helpers.
Does not block VOD processing.  External platform endpoints can change; failures return [].
Cluster human-created clip timestamps when callers can map them into source-VOD seconds.
"""
from dataclasses import dataclass
from statistics import median
@dataclass
class OracleCluster:
    center: float; start: float; end: float; count: int; sources: list

def cluster_offsets(offsets, radius=10.0, min_count=4):
    vals=sorted((float(t),src) for t,src in offsets if t is not None)
    out=[]; i=0
    while i<len(vals):
        base=vals[i][0]; group=[]; j=i
        while j<len(vals) and vals[j][0]-base <= radius*2:
            group.append(vals[j]); j+=1
        if len(group)>=min_count:
            c=median([x[0] for x in group]); out.append(OracleCluster(c,max(0,c-8),c+8,len(group),[x[1] for x in group]))
        i=max(i+1,j)
    return out

def missing_clusters(clusters, candidates, tolerance=15.0):
    return [o for o in clusters if not any((c.start-tolerance)<=o.center<=(c.end+tolerance) for c in candidates)]
