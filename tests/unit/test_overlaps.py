# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import numpy as np

from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.dataset import Calibration, Dataset, SourceImage
from afmlabel.domain.project import ClassDefinition, ClassOntology, SemanticRole
from afmlabel.domain.region import OverlapReview, Region, RegionSet
from afmlabel.overlaps.engine import (
    build_overlap_report,
    compose,
    missing_components,
    overlap_groups,
    prune_stale_reviews,
)
from afmlabel.persistence.rle import encode_coco
from afmlabel.validation.engine import boundary_fill_mask, validate

H, W = 32, 32


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


def ontology(boundary: bool = False) -> ClassOntology:
    classes = [
        ClassDefinition(class_id=1, stable_key="a", name="A", display_color="#ff0000"),
        ClassDefinition(class_id=2, stable_key="b", name="B", display_color="#00ff00"),
        ClassDefinition(class_id=3, stable_key="c", name="C", display_color="#0000ff"),
    ]
    if boundary:
        classes.append(
            ClassDefinition(
                class_id=9,
                stable_key="boundary",
                name="Boundary",
                display_color="#ffffff",
                semantic_role=SemanticRole.BOUNDARY,
            )
        )
    return ClassOntology(classes=classes, next_class_id=10)


def source(modality: str) -> SourceImage:
    return SourceImage(
        channel=modality,
        stored_filename=f"{modality}.tiff",
        original_filename=f"{modality}.tiff",
        sha256="0" * 64,
        size_bytes=1,
        width=W,
        height=H,
        dtype="uint16",
        bit_depth=16,
        channels=1,
        format="tiff",
        calibration=Calibration(),
        imported_at=utc_now_iso(),
    )


def dataset() -> Dataset:
    now = utc_now_iso()
    return Dataset(
        dataset_id="ds_x",
        project_id="proj_x",
        name="x",
        pairing_key="x",
        channels=[source("topography"), source("friction")],
        width=W,
        height=H,
        created_at=now,
        modified_at=now,
    )


def test_two_region_overlap_and_highest_z_wins() -> None:
    big = region("reg_1", 1, 1, block(0, 32, 0, 32))
    small = region("reg_2", 2, 2, block(8, 16, 8, 16))
    rs = RegionSet(dataset_id="ds_x", regions=[big, small])
    comp = compose(rs, ontology(), H, W)
    assert comp.label_map[10, 10] == 2 and comp.label_map[0, 0] == 1
    assert comp.multiplicity.max() == 2
    groups = overlap_groups(comp, rs)
    assert len(groups) == 1
    g = groups[0]
    assert g.pixel_count == 64 and g.max_multiplicity == 2
    assert g.region_ids == ["reg_1", "reg_2"] and g.winning_region_id == "reg_2"
    assert g.bbox_xywh == [8, 8, 8, 8]
    assert g.review_state == "unreviewed"

    # Flip the order: the big region now hides the small one, but the small
    # region is still present and the group id is unchanged.
    big.z_order, small.z_order = 2, 1
    comp2 = compose(rs, ontology(), H, W)
    assert comp2.label_map[10, 10] == 1
    assert overlap_groups(comp2, rs)[0].group_id == g.group_id
    assert [r.region_id for r in rs.regions] == ["reg_1", "reg_2"]


def test_three_region_overlap_multiplicity_and_groups() -> None:
    a = region("reg_a", 1, 1, block(0, 20, 0, 20))
    b = region("reg_b", 2, 2, block(10, 30, 10, 30))
    c = region("reg_c", 3, 3, block(15, 18, 15, 18))
    far = region("reg_d", 1, 4, block(28, 32, 0, 4))
    far2 = region("reg_e", 2, 5, block(30, 32, 2, 4))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b, c, far, far2])
    comp = compose(rs, ontology(), H, W)
    groups = overlap_groups(comp, rs)
    assert len(groups) == 2
    main = next(g for g in groups if "reg_c" in g.region_ids)
    assert main.max_multiplicity == 3
    assert main.region_ids == ["reg_a", "reg_b", "reg_c"]
    assert main.class_ids == [1, 2, 3]
    other = next(g for g in groups if g is not main)
    assert (
        other.region_ids == ["reg_d", "reg_e"]
        and other.max_multiplicity == 2
        and other.pixel_count == 4
    )


def test_group_id_changes_when_coverage_changes_and_reviews_are_pruned() -> None:
    a = region("reg_a", 1, 1, block(0, 20, 0, 20))
    b = region("reg_b", 2, 2, block(10, 30, 10, 30))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b])
    first = overlap_groups(compose(rs, ontology(), H, W), rs)[0]
    rs.overlap_reviews[first.group_id] = OverlapReview(state="reviewed_intentional")
    assert (
        overlap_groups(compose(rs, ontology(), H, W), rs)[0].review_state == "reviewed_intentional"
    )
    # Edit reg_b so the overlapping pixels change: new group id, review gone.
    b.coverage_rle = encode_coco(block(12, 30, 12, 30))
    groups = overlap_groups(compose(rs, ontology(), H, W), rs)
    assert groups[0].group_id != first.group_id
    assert groups[0].review_state == "unreviewed"
    assert prune_stale_reviews(rs, groups) == [first.group_id]
    assert rs.overlap_reviews == {}


def test_hidden_and_disabled_regions_are_not_composed() -> None:
    a = region("reg_a", 1, 1, block(0, 32, 0, 32))
    hidden = region("reg_h", 2, 2, block(0, 10, 0, 10), visible=False)
    ont = ontology()
    ont.classes[2] = ont.classes[2].model_copy(update={"enabled": False})
    disabled = region("reg_d", 3, 3, block(20, 30, 20, 30))
    rs = RegionSet(dataset_id="ds_x", regions=[a, hidden, disabled])
    comp = compose(rs, ont, H, W)
    assert comp.label_map[5, 5] == 1 and comp.label_map[25, 25] == 1
    assert overlap_groups(comp, rs) == []
    report = build_overlap_report("ds_x", 1, rs, ont, H, W)
    assert report.hidden_region_ids == ["reg_h"]


def test_missing_pixels_are_counted_exactly() -> None:
    a = region("reg_a", 1, 1, block(0, 32, 0, 16))
    b = region("reg_b", 2, 2, block(0, 16, 16, 32))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b])
    comp = compose(rs, ontology(), H, W)
    cov = comp.coverage()
    assert cov.total_pixels == 1024 and cov.labeled_pixels == 768 and cov.unlabeled_pixels == 256
    assert cov.coverage_percent == 75.0
    components, count = missing_components(comp)
    assert (
        count == 1
        and components[0].pixel_count == 256
        and components[0].bbox_xywh == [16, 16, 16, 16]
    )


def test_validation_blocks_and_warns_correctly() -> None:
    a = region("reg_a", 1, 1, block(0, 32, 0, 32))
    b = region("reg_b", 2, 2, block(8, 16, 8, 16))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b])
    report, _ = validate(
        dataset(),
        rs,
        ontology(),
        project_revision=1,
        boundary_mode="disabled",
        require_overlap_review=True,
    )
    assert report.status == "blocked"
    assert [i.code for i in report.blocking] == ["OVERLAP_UNREVIEWED"]
    assert report.blocking[0].group_ids and report.blocking[0].region_ids == ["reg_a", "reg_b"]

    # Without review requirement the same overlap is only a warning.
    relaxed, _ = validate(
        dataset(),
        rs,
        ontology(),
        project_revision=1,
        boundary_mode="disabled",
        require_overlap_review=False,
    )
    assert relaxed.status == "valid" and [w.code for w in relaxed.warnings] == [
        "OVERLAP_UNREVIEWED"
    ]
    assert relaxed.class_pixel_histogram == {1: 1024 - 64, 2: 64}
    assert relaxed.composition_sha256

    # Accept the overlap as intentional: valid with a warning; deterministic hash.
    gid = report.blocking[0].group_ids[0]
    rs.overlap_reviews[gid] = OverlapReview(
        state="reviewed_intentional", region_ids=["reg_a", "reg_b"]
    )
    accepted, comp = validate(
        dataset(),
        rs,
        ontology(),
        project_revision=1,
        boundary_mode="disabled",
        require_overlap_review=True,
    )
    assert accepted.status == "valid"
    assert [w.code for w in accepted.warnings] == ["OVERLAP_MULTIPLE"]
    assert accepted.composition_sha256 == relaxed.composition_sha256
    assert (comp.label_map != 0).all()

    # Missing pixels, hidden regions, disabled classes and Boundary policy block.
    b.visible = False
    a.coverage_rle = encode_coco(block(0, 32, 0, 16))
    blocked, _ = validate(
        dataset(),
        rs,
        ontology(),
        project_revision=1,
        boundary_mode="disabled",
        require_overlap_review=True,
    )
    codes = {i.code for i in blocked.blocking}
    assert {"REGION_HIDDEN", "UNLABELED_PIXELS"} <= codes
    assert blocked.composition_sha256 is None


def test_boundary_policy_and_fill_never_overwrites_labels() -> None:
    a = region("reg_a", 1, 1, block(0, 32, 0, 16))
    edge = region("reg_e", 9, 2, block(0, 32, 16, 20))
    rs = RegionSet(dataset_id="ds_x", regions=[a, edge])
    ont = ontology(boundary=True)
    # disabled mode forbids Boundary regions.
    disabled, _ = validate(
        dataset(),
        rs,
        ont,
        project_revision=1,
        boundary_mode="disabled",
        require_overlap_review=True,
    )
    assert "BOUNDARY_POLICY_VIOLATION" in {i.code for i in disabled.blocking}
    # explicit_class allows them; still missing pixels.
    explicit, comp = validate(
        dataset(),
        rs,
        ont,
        project_revision=1,
        boundary_mode="explicit_class",
        require_overlap_review=True,
    )
    assert [i.code for i in explicit.blocking] == ["UNLABELED_PIXELS"]
    fill = boundary_fill_mask(comp)
    assert int(fill.sum()) == 32 * 12
    assert not (fill & (comp.label_map != 0)).any()
    # fill mode without a Boundary class is a policy violation.
    no_boundary, _ = validate(
        dataset(),
        RegionSet(dataset_id="ds_x", regions=[a]),
        ontology(),
        project_revision=1,
        boundary_mode="fill_unlabeled_after_approval",
        require_overlap_review=True,
    )
    assert "BOUNDARY_POLICY_VIOLATION" in {i.code for i in no_boundary.blocking}
    # After an approved fill at the bottom of the z-order everything is valid.
    filled = region("reg_f", 9, 0, fill, source="boundary_fill")
    rs.regions.append(filled)
    rs.renumber_z()
    done, comp2 = validate(
        dataset(),
        rs,
        ont,
        project_revision=1,
        boundary_mode="fill_unlabeled_after_approval",
        require_overlap_review=True,
    )
    assert done.status == "valid"
    assert [w.code for w in done.warnings] == ["BOUNDARY_POLICY_VIOLATION"]
    assert (
        comp2.label_map[0, 0] == 1 and comp2.label_map[0, 18] == 9 and comp2.label_map[0, 25] == 9
    )


def test_composition_is_deterministic() -> None:
    a = region("reg_a", 1, 1, block(0, 32, 0, 32))
    b = region("reg_b", 2, 2, block(4, 20, 4, 20))
    c = region("reg_c", 3, 3, block(10, 30, 10, 30))
    rs = RegionSet(dataset_id="ds_x", regions=[a, b, c])
    first = compose(rs, ontology(), H, W)
    second = compose(RegionSet(dataset_id="ds_x", regions=[c, a, b]), ontology(), H, W)
    assert first.sha256() == second.sha256()
    assert np.array_equal(first.label_map, second.label_map)
