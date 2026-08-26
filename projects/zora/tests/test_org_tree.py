"""Tests for the community-tree walk behind the org_unit mirror.

A fake client answers fetch_resource from a canned 3-level tree, so the walk's
parent/depth/faculty bookkeeping and pagination are exercised offline.
"""

from __future__ import annotations

import pytest

from themis_zora import config
from themis_zora.zora_client import iter_org_tree

ROOT = config.ZoraSettings.ZORA_ROOT_COMMUNITY_UUID


def _community(uuid: str, name: str) -> dict:
    return {"uuid": uuid, "name": name, "handle": f"20.500.14742/{uuid}", "metadata": {}}


def _page(embed_key: str, entries: list[dict], *, total_pages: int = 1, page: int = 0) -> dict:
    return {
        "_embedded": {embed_key: entries},
        "page": {"totalPages": total_pages, "number": page},
    }


class FakeClient:
    """Answers the exact URLs iter_org_tree asks for, from a canned tree."""

    def __init__(self, subcommunities: dict, collections: dict, root: dict):
        self._subcommunities = subcommunities  # parent uuid -> list of community dicts
        self._collections = collections  # community uuid -> list of collection dicts
        self._root = root

    def fetch_resource(self, url: str, params: dict | None = None):
        if url.endswith(f"/core/communities/{ROOT}"):
            return self._root
        for uuid, children in self._subcommunities.items():
            if url.endswith(f"/core/communities/{uuid}/subcommunities"):
                return _page("subcommunities", children)
        for uuid, colls in self._collections.items():
            if url.endswith(f"/core/communities/{uuid}/collections"):
                return _page("collections", colls)
        # Unknown node: empty lists, like the live API for a leaf.
        if url.endswith("/subcommunities"):
            return _page("subcommunities", [])
        if url.endswith("/collections"):
            return _page("collections", [])
        return None


def _three_level_client() -> FakeClient:
    root = _community(ROOT, "University of Zurich")
    fac1 = _community("fac-1", "01 Faculty of Theology")
    fac2 = _community("fac-2", "03 Faculty of Economics")
    inst = _community("inst-1", "Department of Finance")
    return FakeClient(
        subcommunities={ROOT: [fac1, fac2], "fac-2": [inst]},
        collections={
            "fac-2": [{"uuid": "coll-fac-2", "name": "Publications of Faculty of Economics"}],
            "inst-1": [
                {"uuid": "extra-1", "name": "Some working papers"},
                {"uuid": "coll-inst-1", "name": "Publications of Department of Finance"},
            ],
        },
        root=root,
    )


def test_walk_yields_every_community_with_parent_depth_faculty():
    walked = list(iter_org_tree(_three_level_client()))

    by_uuid = {
        community["uuid"]: (parent, depth, faculty)
        for community, parent, depth, faculty, _ in walked
    }
    assert by_uuid == {
        ROOT: (None, 0, None),
        "fac-1": (ROOT, 1, "fac-1"),
        "fac-2": (ROOT, 1, "fac-2"),
        "inst-1": ("fac-2", 2, "fac-2"),
    }


def test_walk_hands_over_each_communitys_collections():
    walked = {
        community["uuid"]: collections
        for community, _, _, _, collections in iter_org_tree(_three_level_client())
    }
    assert walked[ROOT] == []
    assert [c["uuid"] for c in walked["inst-1"]] == ["extra-1", "coll-inst-1"]


def test_walk_paginates_subcommunities():
    """A parent with two pages of children yields all of them."""
    root = _community(ROOT, "University of Zurich")
    page_children = [_community(f"fac-{i}", f"{i:02d} Faculty {i}") for i in range(3)]

    class PagingClient(FakeClient):
        def fetch_resource(self, url: str, params: dict | None = None):
            if url.endswith(f"/core/communities/{ROOT}/subcommunities"):
                page = (params or {}).get("page", 0)
                chunk = page_children[page * 2 : page * 2 + 2]
                return _page("subcommunities", chunk, total_pages=2, page=page)
            return super().fetch_resource(url, params)

    client = PagingClient(subcommunities={}, collections={}, root=root)
    walked = list(iter_org_tree(client))

    assert [c["uuid"] for c, *_ in walked] == [ROOT, "fac-0", "fac-1", "fac-2"]


def test_walk_raises_on_missing_root():
    class BrokenClient(FakeClient):
        def fetch_resource(self, url: str, params: dict | None = None):
            return None

    client = BrokenClient(subcommunities={}, collections={}, root={})
    with pytest.raises(RuntimeError, match="root community"):
        list(iter_org_tree(client))


def test_walk_raises_on_a_failed_page_instead_of_truncating():
    """A half-walked tree must fail the run, not commit a silently pruned snapshot."""
    root = _community(ROOT, "University of Zurich")

    class FlakyClient(FakeClient):
        def fetch_resource(self, url: str, params: dict | None = None):
            if url.endswith(f"/core/communities/{ROOT}/collections"):
                return None
            return super().fetch_resource(url, params)

    client = FlakyClient(subcommunities={}, collections={}, root=root)
    with pytest.raises(RuntimeError, match="page"):
        list(iter_org_tree(client))
