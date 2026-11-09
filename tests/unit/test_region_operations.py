# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Unit tests for the upgrade's structural region operations:
Subtract by Order, Delete Selected, snapshot restore (undo transport) and
the exactly-one-winner composition invariant, on tiny golden rasters."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from afmlabel.annotations.service import RegionService, load_region_set
from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.project import ClassDefinition, ClassOntology
from afmlabel.domain.region import OverlapReview, Region, RegionSet
from afmlabel.errors import AppError
from afmlabel.imports.batches import BatchStore
from afmlabel.overlaps.engine import compose
from afmlabel.persistence.rle import decode_coco, encode_coco
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.store import ProjectStore
from tests.fixtures.images import write_pair

H, W = 8, 8


def block(y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    mask = np.zeros((H, W), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


def region(rid: str, class_id: int, z: int, mask: np.ndarray, **kwargs: object) -> Region:
    now = utc_now_iso()
    return Region(
        region_id=rid,
        dataset_id="ds_x",
        class_id=class_id,
        z_order=z,
        coverage_rle=encode_coco(mask),
        area=int(mask.sum()),
        created_at=now,
        modified_at=now,
        **kwargs,  # type: ignore[arg-type]
    )


def ontology() -> ClassOntology:
    return ClassOntology(
        classes=[
            ClassDefinition(class_id=1, stable_key="a", name="A", display_color="#ff0000"),
            ClassDefinition(class_id=2, stable_key="b", name="B", display_color="#00ff00"),
            ClassDefinition(class_id=3, stable_key="c", name="C", display_color="#0000ff"),
        ],
        next_class_id=4,
    )


# ------------------------------------------------------------ subtract plan


def test_subtract_plan_two_regions_top_keeps_pixels() -> None:
    low = region("reg_000001", 1, 1, block(0, 8, 0, 8))
    top = region("reg_000002", 2, 2, block(2, 6, 2, 6))
    rs = RegionSet(dataset_id="ds_x", regions=[low, top])
    plan = RegionService.subtract_plan(rs, ["reg_000001", "reg_000002"])
    assert [p["region_id"] for p in plan] == ["reg_000002", "reg_000001"]  # top first
    assert plan[0]["pixels_removed"] == 0 and plan[0]["pixels_after"] == 16
    assert plan[1]["pixels_before"] == 64 and plan[1]["pixels_after"] == 48
    assert not (plan[0]["mask"] & plan[1]["mask"]).any()


def test_subtract_plan_three_regions_chain_and_full_cover() -> None:
    a = region("reg_000001", 1, 1, block(0, 8, 0, 8))
    b = region("reg_000002", 2, 2, block(0, 4, 0, 8))
    c = region("reg_000003", 3, 3, block(0, 8, 0, 4))
    hidden = region("reg_000004", 1, 4, block(1, 3, 1, 3))  # fully inside c and b
    rs = RegionSet(dataset_id="ds_x", regions=[a, b, c, hidden])
    plan = RegionService.subtract_plan(rs, ["reg_000001", "reg_000002", "reg_000003", "reg_000004"])
    by_id = {p["region_id"]: p for p in plan}
    assert by_id["reg_000004"]["pixels_removed"] == 0  # highest keeps everything
    assert by_id["reg_000003"]["pixels_after"] == 32 - 4
    assert (
        by_id["reg_000002"]["pixels_after"] == 32 - 16
    )  # loses c's top-left quarter (+hidden inside)
    assert by_id["reg_000001"]["pixels_after"] == 64 - 32 - 16
    masks = [p["mask"] for p in plan]
    union_count = sum(int(m.sum()) for m in masks)
    assert union_count == int(np.logical_or.reduce(masks).sum())  # pairwise disjoint


def test_subtract_plan_detects_region_that_becomes_empty_and_ignores_unselected() -> None:
    a = region("reg_000001", 1, 1, block(0, 8, 0, 8))
    b = region("reg_000002", 2, 2, block(2, 4, 2, 4))
    c = region("reg_000003", 3, 3, block(0, 8, 0, 8))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b, c])
    plan = RegionService.subtract_plan(rs, ["reg_000002", "reg_000003"])
    assert [p["region_id"] for p in plan] == ["reg_000003", "reg_000002"]
    assert plan[1]["becomes_empty"] is True and plan[1]["pixels_after"] == 0
    # reg_000001 was not selected and is not part of the plan.
    assert all(p["region_id"] != "reg_000001" for p in plan)


def test_subtract_plan_needs_two_regions() -> None:
    rs = RegionSet(dataset_id="ds_x", regions=[region("reg_000001", 1, 1, block(0, 8, 0, 8))])
    with pytest.raises(AppError):
        RegionService.subtract_plan(rs, ["reg_000001"])


# ----------------------------------------------------------- with a store


@pytest.fixture
def service(tmp_path: Path) -> tuple[RegionService, str, str, ClassOntology]:
    root = tmp_path / "ws"
    projects = ProjectStore(root, fsync=False)
    batches = BatchStore(root, max_file_size_bytes=10_000_000, chunk_size=65536)
    datasets = DatasetStore(projects, batches, fsync=False)
    project = projects.create_project("p")
    ont = ontology()

    def add_classes(p: object, o: ClassOntology) -> None:
        o.classes = list(ont.classes)
        o.next_class_id = ont.next_class_id

    projects.mutate_project(project.project_id, 1, add_classes, operation="test")
    batch = batches.create(project.project_id)
    topo, fric = write_pair(tmp_path / "in", "500", height=H, width=W)
    with topo.open("rb") as t:
        batches.add_file(batch, "topography", topo.name, t)
    with fric.open("rb") as f:
        batches.add_file(batch, "friction", fric.name, f)
    preview = batches.raw_preview(batch)
    row = preview.rows[0]
    assert row.status == "ok" and set(row.file_ids()) == {"topography", "friction"}
    created = datasets.commit_batch(
        project.project_id,
        batch,
        [(row.pairing_key, row.file_ids())],
        pairing_method="key",
        expected_revision=2,
        actor="t",
    )
    return RegionService(datasets, fsync=False), project.project_id, created[0].dataset_id, ont


def _create(svc: RegionService, pid: str, dsid: str, ont: ClassOntology, cid: int, m: np.ndarray):
    dataset, _, reg = svc.create_region(
        pid, dsid, None, ontology=ont, class_id=cid, mask=m, actor="t"
    )
    return reg.region_id


def test_subtract_by_order_persists_and_removes_empty_only_when_allowed(service) -> None:
    svc, pid, dsid, ont = service
    a = _create(svc, pid, dsid, ont, 1, block(0, 8, 0, 8))
    b = _create(svc, pid, dsid, ont, 2, block(2, 4, 2, 4))
    c = _create(svc, pid, dsid, ont, 3, block(0, 8, 0, 8))
    with pytest.raises(AppError) as excinfo:
        svc.subtract_by_order(pid, dsid, [a, b, c], None, allow_empty_removal=False, actor="t")
    assert excinfo.value.details["empty_region_ids"] == [b, a]
    dataset, region_set, outcome = svc.subtract_by_order(
        pid, dsid, [a, b, c], None, allow_empty_removal=True, actor="t"
    )
    assert [r.region_id for r in region_set.regions] == [c]
    assert [o["region_id"] for o in outcome] == [c, b, a]
    assert dataset.validated_revision is None
    comp = compose(region_set, ont, H, W)
    assert (comp.multiplicity <= 1).all()


def test_subtract_by_order_refuses_locked_loser_and_keeps_unselected(service) -> None:
    svc, pid, dsid, ont = service
    a = _create(svc, pid, dsid, ont, 1, block(0, 8, 0, 8))
    b = _create(svc, pid, dsid, ont, 2, block(0, 4, 0, 8))
    other = _create(svc, pid, dsid, ont, 3, block(6, 8, 6, 8))
    svc.update_region(pid, dsid, a, None, ontology=ont, locked=True, actor="t")
    with pytest.raises(AppError) as excinfo:
        svc.subtract_by_order(pid, dsid, [a, b], None, allow_empty_removal=True, actor="t")
    assert excinfo.value.code == "REGION_LOCKED"
    svc.update_region(pid, dsid, a, None, ontology=ont, locked=False, actor="t")
    _, region_set, _ = svc.subtract_by_order(
        pid, dsid, [a, b], None, allow_empty_removal=True, actor="t"
    )
    by_id = {r.region_id: r for r in region_set.regions}
    assert by_id[a].area == 32 and by_id[b].area == 32
    assert by_id[other].area == 4  # untouched
    assert "subtracted_by_order" in by_id[a].provenance


def test_delete_regions_keeps_locked_and_reports(service) -> None:
    svc, pid, dsid, ont = service
    a = _create(svc, pid, dsid, ont, 1, block(0, 2, 0, 2))
    b = _create(svc, pid, dsid, ont, 1, block(2, 4, 2, 4))
    svc.update_region(pid, dsid, b, None, ontology=ont, locked=True, actor="t")
    _, region_set, deleted, kept = svc.delete_regions(pid, dsid, [a, b], None, actor="t")
    assert deleted == [a] and kept == [b]
    assert [r.region_id for r in region_set.regions] == [b]
    with pytest.raises(AppError) as excinfo:
        svc.delete_regions(pid, dsid, [b], None, actor="t")
    assert excinfo.value.code == "REGION_LOCKED"


def test_restore_snapshot_brings_back_ids_masks_order_and_reviews(service) -> None:
    svc, pid, dsid, ont = service
    a = _create(svc, pid, dsid, ont, 1, block(0, 8, 0, 8))
    b = _create(svc, pid, dsid, ont, 1, block(1, 3, 1, 3))
    c = _create(svc, pid, dsid, ont, 2, block(5, 7, 5, 7))
    svc.reorder(pid, dsid, None, [b, a, c], actor="t")
    svc.update_region(pid, dsid, c, None, ontology=ont, locked=True, name="edge", actor="t")
    _, before = svc.load(pid, dsid)
    before.overlap_reviews["ovl_x"] = OverlapReview(state="reviewed_intentional", region_ids=[a, b])
    snapshot = [r.model_copy(deep=True) for r in before.regions]
    reviews = {k: v.model_dump() for k, v in before.overlap_reviews.items()}

    # Merge a+b (destructive), then restore the snapshot: exact state back.
    _, merged, target = svc.merge_regions(pid, dsid, [a, b], None, actor="t")
    assert [r.region_id for r in merged.regions] != [r.region_id for r in snapshot]
    dataset, restored = svc.restore_snapshot(
        pid,
        dsid,
        None,
        ontology=ont,
        regions=snapshot,
        overlap_reviews=reviews,
        next_region_seq=before.next_region_seq,
        reason="undo merge",
        actor="t",
    )
    assert [r.region_id for r in restored.sorted_by_z()] == [b, a, c]
    assert [r.z_order for r in restored.sorted_by_z()] == [1, 2, 3]
    for original in snapshot:
        again = restored.by_id(original.region_id)
        assert again is not None
        assert np.array_equal(decode_coco(again.coverage_rle), decode_coco(original.coverage_rle))
        assert again.class_id == original.class_id and again.locked == original.locked
        assert again.name == original.name and again.provenance == original.provenance
    assert restored.overlap_reviews["ovl_x"].state == "reviewed_intentional"
    assert restored.next_region_seq >= 4
    assert dataset.validated_revision is None
    # A new region after the restore never reuses an id.
    d = _create(svc, pid, dsid, ont, 2, block(0, 1, 0, 1))
    assert d not in {a, b, c}


def test_restore_snapshot_rejects_unknown_class_and_wrong_size(service) -> None:
    svc, pid, dsid, ont = service
    bad_class = region("reg_000001", 9, 1, block(0, 2, 0, 2))
    with pytest.raises(AppError) as excinfo:
        svc.restore_snapshot(
            pid,
            dsid,
            None,
            ontology=ont,
            regions=[bad_class],
            overlap_reviews={},
            next_region_seq=None,
            reason="x",
            actor="t",
        )
    assert excinfo.value.code == "CLASS_REFERENCE_INVALID"
    wrong = region("reg_000001", 1, 1, np.ones((4, 4), dtype=bool))
    with pytest.raises(AppError):
        svc.restore_snapshot(
            pid,
            dsid,
            None,
            ontology=ont,
            regions=[wrong],
            overlap_reviews={},
            next_region_seq=None,
            reason="x",
            actor="t",
        )
    # Nothing was written by the failed attempts.
    ddir = svc._datasets.dataset_dir(pid, dsid)  # noqa: SLF001
    assert load_region_set(ddir, dsid).regions == []


def test_composition_has_exactly_one_winner_per_pixel_after_overlaps() -> None:
    a = region("reg_000001", 1, 1, block(0, 8, 0, 8))
    b = region("reg_000002", 2, 2, block(0, 4, 0, 8))
    c = region("reg_000003", 3, 3, block(0, 8, 0, 4))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b, c])
    comp = compose(rs, ontology(), H, W)
    expected = np.full((H, W), 1, dtype=np.int32)
    expected[0:4, :] = 2
    expected[:, 0:4] = 3  # c is highest: wins the whole left half
    assert np.array_equal(comp.label_map, expected)
    assert (comp.label_map > 0).all()
    winners = comp.winner_map
    assert set(np.unique(winners).tolist()) == {1, 2, 3}
    # Each pixel maps to exactly one region index (an int, never a set).
    assert winners.dtype == np.int32 and winners.shape == (H, W)
