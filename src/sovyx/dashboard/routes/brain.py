"""Brain graph + search endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from sovyx.dashboard.routes._deps import verify_token

router = APIRouter(prefix="/api", dependencies=[Depends(verify_token)])


class BrainGraphResponse(BaseModel):
    """Response of `GET /api/brain/graph` (Mission C C.4).

    Node + link shapes vary by graph layout; typed as opaque dicts
    for forward-additive evolution. Forward-additive via
    ``extra="allow"`` (anti-pattern #40)."""

    model_config = ConfigDict(extra="allow")
    nodes: list[dict[str, object]] = []
    links: list[dict[str, object]] = []


class BrainSearchResponse(BaseModel):
    """Response of `GET /api/brain/search` (Mission C C.4)."""

    model_config = ConfigDict(extra="allow")
    results: list[dict[str, object]] = []
    query: str = ""


class BrainVectorSearchResponse(BaseModel):
    """Response of `GET /api/brain/search/vector` (Mission C C.4).

    ``vector_available`` distinguishes the no-sqlite-vec fallback
    from a real zero-result match."""

    model_config = ConfigDict(extra="allow")
    results: list[dict[str, object]] = []
    query: str = ""
    vector_available: bool | None = None


@router.get("/brain/graph", response_model=BrainGraphResponse)
async def get_brain_graph(
    request: Request,
    limit: int = Query(default=200, ge=0, le=1000),
) -> JSONResponse:
    """Brain knowledge graph (nodes + links for react-force-graph-2d)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.brain import get_brain_graph as _get_graph

        graph = await _get_graph(registry, limit=limit)
        return JSONResponse(graph)
    return JSONResponse({"nodes": [], "links": []})


@router.get("/brain/search", response_model=BrainSearchResponse)
async def brain_search(
    request: Request,
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
) -> JSONResponse:
    """Semantic search over brain concepts (hybrid FTS+vector)."""
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.brain import search_brain

        results = await search_brain(registry, q, limit=limit)
        return JSONResponse({"results": results, "query": q})
    return JSONResponse({"results": [], "query": q})


@router.get("/brain/search/vector", response_model=BrainVectorSearchResponse)
async def brain_search_vector(
    request: Request,
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=10, ge=1, le=100),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
) -> JSONResponse:
    """Pure vector similarity search (KNN only, no FTS5).

    Returns concepts ranked by embedding cosine similarity to the query.
    Requires sqlite-vec; returns ``vector_available: false`` when the
    extension is not loaded.
    """
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        from sovyx.dashboard.brain import search_brain_vector

        result = await search_brain_vector(registry, q, limit=limit, min_score=min_score)
        return JSONResponse(result)
    return JSONResponse({"results": [], "query": q, "vector_available": False})
