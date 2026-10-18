"""Sub-query generation from TripSpec + pre/post filtered hybrid retrieval (T2.4)."""

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from tripmate_planner.retrieval.bm25_index import get_bm25
from tripmate_planner.retrieval.corpus import PoiDoc, build_corpus
from tripmate_planner.retrieval.dense_index import get_dense
from tripmate_planner.retrieval.fusion import rrf
from tripmate_planner.retrieval.rerank import get_reranker
from tripmate_planner.spec.tripspec import TripSpec

FIELD = {"attraction": {"type": "type"}, "restaurant": {"cuisine": "cuisine"}, "hotel": {"feature": "featurehoteltype"}}


@dataclass(frozen=True)
class Filter:
    field: str  # meta column name
    op: str  # eq | contains | not_in
    value: Any


@dataclass
class SubQuery:
    sq_id: str
    kind: str
    text: str
    filters: list[Filter]
    pool_size: int
    constraint_ref: str | None


@dataclass
class Candidate:
    doc: PoiDoc
    rel_rank: int
    sources: set[str] = field(default_factory=set)


@dataclass
class RequiredGroup:
    constraint_ref: str
    kind: str
    doc_ids: list[str]


@dataclass
class CandidatePools:
    attractions: list[Candidate] = field(default_factory=list)
    restaurants: list[Candidate] = field(default_factory=list)
    hotels: list[Candidate] = field(default_factory=list)
    required_groups: list[RequiredGroup] = field(default_factory=list)
    shortfalls: list[dict[str, Any]] = field(default_factory=list)
    ms: float = 0.0

    def by_kind(self, kind: str) -> list[Candidate]:
        return {"attraction": self.attractions, "restaurant": self.restaurants, "hotel": self.hotels}[kind]


def _match(doc: PoiDoc, flt: Filter) -> bool:
    value = doc.meta.get(flt.field) if flt.field != "name" else doc.name
    if flt.op == "eq":
        return bool(value == flt.value)
    if flt.op == "contains":
        return flt.value in str(value)
    if flt.op == "not_in":
        return value not in flt.value
    raise ValueError(flt.op)


def build_subqueries(spec: TripSpec, base_text: str, pools: dict[str, int]) -> list[SubQuery]:
    city = spec.target_city or ""
    avoid = [Filter("cuisine", "not_in", tuple(spec.avoid_cuisines))] if spec.avoid_cuisines else []
    sqs: list[SubQuery] = []
    per, named = int(pools["per_constraint"]), int(pools["per_named"])
    for t in spec.attraction_types:
        sqs.append(SubQuery(f"atype:{t}", "attraction", f"{city} {t}", [Filter("type", "eq", t)], per, f"attraction_type:{t}"))
    for n in spec.must_visit:
        sqs.append(SubQuery(f"apos:{n}", "attraction", n, [Filter("name", "eq", n)], named, f"position:{n}"))
    for c in spec.cuisines:
        sqs.append(SubQuery(f"rtype:{c}", "restaurant", c, [Filter("cuisine", "eq", c), *avoid], per, f"restaurant_type:{c}"))
    for n in spec.must_eat_at:
        sqs.append(SubQuery(f"rpos:{n}", "restaurant", n, [Filter("name", "eq", n)], named, f"position:{n}"))
    for f in spec.foods:
        sqs.append(SubQuery(f"food:{f}", "restaurant", f, [Filter("recommendedfood", "contains", f), *avoid], per, f"food:{f}"))
    for h in spec.hotel_features:
        sqs.append(SubQuery(f"htype:{h}", "hotel", h, [Filter("featurehoteltype", "eq", h)], int(pools["hotel_feature"]),
                            f"accommodation_type:{h}"))
    if spec.must_stay_at:
        sqs.append(SubQuery(f"hpos:{spec.must_stay_at}", "hotel", spec.must_stay_at,
                            [Filter("name", "eq", spec.must_stay_at)], named, f"position:{spec.must_stay_at}"))
    general = " ".join([base_text, *spec.notes])
    sqs.append(SubQuery("gen:attraction", "attraction", general, [], int(pools["attraction_general"]), None))
    sqs.append(SubQuery("gen:restaurant", "restaurant", general, list(avoid), int(pools["restaurant_general"]), None))
    room = [Filter("numbed", "eq", spec.room_type)] if spec.room_type else []
    sqs.append(SubQuery("gen:hotel", "hotel", general, room, int(pools["hotel_general"]), None))
    return sqs


class Retriever:
    def __init__(self, rcfg: dict[str, Any]) -> None:
        self.cfg = rcfg
        self.docs = build_corpus()
        self.bm25 = get_bm25(float(rcfg["bm25"]["k1"]), float(rcfg["bm25"]["b"]))
        mode = rcfg["mode"]
        self.dense = get_dense(rcfg["query_instruction"]) if mode in ("dense", "hybrid") else None
        rr = rcfg["rerank"]
        self.reranker = get_reranker(int(rr["max_length"]), int(rr["batch_size"])) if rr["enabled"] else None

    def search(self, city: str, kind: str, text: str, filters: list[Filter], limit: int,
               filter_mode: str | None = None) -> list[int]:
        """Return global doc indexes, best first."""
        filter_mode = filter_mode or self.cfg["filter_mode"]
        idx, bm = self.bm25.scores(city, kind, text)
        idx_arr = np.asarray(idx)
        if filter_mode == "pre" and filters:
            keep = np.array([all(_match(self.docs[i], f) for f in filters) for i in idx], dtype=bool)
        else:
            keep = np.ones(len(idx), dtype=bool)
        topn = int(self.cfg["channel_topn"])
        rankings: list[list[int]] = []
        mode = self.cfg["mode"]
        if mode in ("bm25", "hybrid"):
            order = [j for j in np.argsort(-bm, kind="stable") if keep[j] and bm[j] > 0][:topn]
            rankings.append([int(idx_arr[j]) for j in order])
        if mode in ("dense", "hybrid") and self.dense is not None:
            ds = self.dense.scores(idx, text)
            order = [j for j in np.argsort(-ds, kind="stable") if keep[j]][:topn]
            rankings.append([int(idx_arr[j]) for j in order])
        fused = [d for d, _ in rrf(rankings, int(self.cfg["rrf_k"]))]
        if filter_mode == "pre" and filters:
            # docs that satisfy hard filters but scored 0 everywhere stay reachable (exact-name constraints)
            fused += [int(idx_arr[j]) for j in np.flatnonzero(keep) if int(idx_arr[j]) not in set(fused)]
        fused = fused[: int(self.cfg["fused_topn"])]
        if filter_mode == "post" and filters:
            fused = [d for d in fused if all(_match(self.docs[d], f) for f in filters)]
        if self.reranker is not None and len(fused) > 1:
            sc = self.reranker.scores(text, [self.docs[d].text for d in fused])
            fused = [d for _, d in sorted(zip(sc, fused, strict=True), key=lambda x: -x[0])]
        return fused[:limit]

    def build_pools(self, spec: TripSpec, base_text: str) -> CandidatePools:
        started = time.perf_counter()
        pools = CandidatePools()
        if spec.target_city is None:
            return pools
        by_doc: dict[str, Candidate] = {}
        for sq in build_subqueries(spec, base_text, self.cfg["pool_sizes"]):
            hits = self.search(spec.target_city, sq.kind, sq.text, sq.filters, sq.pool_size)
            if len(hits) < sq.pool_size and sq.constraint_ref is not None:
                pools.shortfalls.append({"sq_id": sq.sq_id, "returned": len(hits), "wanted": sq.pool_size})
            for rank, d in enumerate(hits, start=1):
                doc = self.docs[d]
                cand = by_doc.get(doc.doc_id)
                if cand is None:
                    cand = by_doc[doc.doc_id] = Candidate(doc=doc, rel_rank=rank)
                    pools.by_kind(doc.kind).append(cand)
                cand.rel_rank = min(cand.rel_rank, rank)
                cand.sources.add(sq.sq_id)
            if sq.constraint_ref is not None:
                pools.required_groups.append(RequiredGroup(sq.constraint_ref, sq.kind,
                                                           [self.docs[d].doc_id for d in hits]))
        for kind in ("attraction", "restaurant", "hotel"):
            pools.by_kind(kind).sort(key=lambda c: (c.rel_rank, c.doc.doc_id))
        pools.ms = (time.perf_counter() - started) * 1000
        return pools
