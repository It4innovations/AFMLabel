#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Create a demo project with synthetic topography/friction pairs and classes.

    python scripts/create_demo_project.py --workspace ./workspace --pairs 3

Uses the same stores as the application (no HTTP), so the result is a
normal project visible after `afmlabel serve`. Synthetic images only; no
real AFM data is involved.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures.images import synthetic_friction, synthetic_topography, write_tiff  # noqa: E402

from afmlabel.annotations.service import RegionService  # noqa: E402
from afmlabel.domain.project import ClassDefinition, ClassOntology, Project  # noqa: E402
from afmlabel.imports.batches import BatchStore  # noqa: E402
from afmlabel.projects.datasets import DatasetStore  # noqa: E402
from afmlabel.projects.store import ProjectStore  # noqa: E402
from afmlabel.settings import load_settings  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument("--name", default="Demo project")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    workspace = args.workspace or settings.workspace.root
    projects = ProjectStore(workspace)
    batches = BatchStore(workspace, max_file_size_bytes=512 * 1024 * 1024, chunk_size=512 * 1024)
    datasets = DatasetStore(projects, batches)
    regions = RegionService(datasets)

    project = projects.create_project(args.name, actor="create_demo_project")

    def add_classes(_: Project, ontology: ClassOntology) -> None:
        for index, (name, color) in enumerate(
            [("Layer A", "#00a398"), ("Layer B", "#fa3253"), ("Buffer", "#83e070")], start=1
        ):
            ontology.classes.append(
                ClassDefinition(
                    class_id=index,
                    stable_key=name.lower().replace(" ", "_"),
                    name=name,
                    display_color=color,
                )
            )
        ontology.next_class_id = 4

    project, _ = projects.mutate_project(
        project.project_id, project.revision, add_classes, operation="class.create"
    )

    staging = workspace / "tmp" / "demo-images"
    batch = batches.create(project.project_id)
    pairs = []
    for index in range(args.pairs):
        key = f"{300 + index}"
        topo = write_tiff(
            staging / f"{key}_topography.tiff",
            synthetic_topography(args.size, args.size, seed=index),
        )
        fric = write_tiff(
            staging / f"{key}_friction.tiff",
            synthetic_friction(args.size, args.size, seed=100 + index),
        )
        with topo.open("rb") as t:
            rt = batches.add_file(batch, "topography", topo.name, t)
        with fric.open("rb") as f:
            rf = batches.add_file(batch, "friction", fric.name, f)
        pairs.append((key, {"topography": rt.file_id, "friction": rf.file_id}))
    created = datasets.commit_batch(
        project.project_id,
        batch,
        pairs,
        pairing_method="key",
        expected_revision=projects.get_project(project.project_id).revision,
        actor="create_demo_project",
    )
    batches.discard(batch.batch_id)

    # One overlapping pair of regions on the first dataset to show the findings panel.
    first = created[0]
    ontology = projects.get_ontology(project.project_id)
    big = np.zeros((first.height, first.width), dtype=bool)
    big[: first.height // 2, :] = True
    small = np.zeros((first.height, first.width), dtype=bool)
    small[first.height // 4 : first.height // 2 + 10, first.width // 4 : first.width // 2] = True
    regions.create_region(
        project.project_id,
        first.dataset_id,
        None,
        ontology=ontology,
        class_id=1,
        mask=big,
        actor=None,
    )
    regions.create_region(
        project.project_id,
        first.dataset_id,
        None,
        ontology=ontology,
        class_id=2,
        mask=small,
        actor=None,
    )
    print(f"created project {project.project_id} ({args.name}) with {len(created)} dataset(s)")
    print(f"open http://127.0.0.1:{settings.server.port}/projects/{project.project_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
