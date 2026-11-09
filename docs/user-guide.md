# AFMLabel user guide

AFMLabel creates scientifically traceable multiclass segmentation ground
truth from co-registered atomic force microscopy (AFM) images. A dataset is
a set of 1 to 64 source images of the same field of view (classically
topography + friction; some instruments also export phase, amplitude or
further channels); all of them share **one** annotation. Regions may
overlap while you work; the exported mask has exactly one class per pixel.

This guide covers the complete workflow from signing in to interpreting an
exported mask. Installation and starting the server are described in the
repository `README.md`. Every screen has contextual help: hover or focus a
control for a short description, click the round `?` next to complex
actions for the extended text, and use **Help** in the header to open this
guide inside the application. Nothing in the Help needs a network
connection; the application never contacts the network at runtime.

## Start and sign in

Open the AFMLabel URL (by default `http://127.0.0.1:9080`) and sign in with
the single application account created by the administrator
(`afmlabel auth set-password`). Sessions expire after a period of
inactivity; you are simply asked to sign in again.

## Create or open a project

The Projects page lists every project. **Create project** asks for a name
and for the **number of images per dataset (channels)**: `2` is the classic
topography + friction pair; choose the number your instrument exports
(1 to 64). A project holds one class ontology, one set of channel
definitions and any number of datasets. The search box filters projects by
name; **Open** goes to the project page.

### Channels

The **Channels** table on the project page defines the source images every
dataset must carry, in display order:

| Column | Meaning |
| --- | --- |
| Label | Name shown on buttons, in the channel navigator, on dataset cards and, for non-classic channels, as the export folder name. |
| Key | Stable machine identifier (lowercase letters, digits, underscore). It names the stored file, the API path segment (`/image/<key>`) and is written into manifests. Derived from the label for new channels; it never changes once saved. |
| Filename tokens | Words removed from an uploaded filename to obtain the pairing key (`239_topography.tiff` → `239`), and used by the *all channels at once* picker to sort files into channels. |

The first two channels default to `topography` (tokens `topography, topo,
height, z, hgt`) and `friction` (tokens `friction, fric, lateral, lat, lfm,
fr`); further channels default to `Channel 3` / `channel_3` with tokens
`channel3, ch3, c3`. Rename them and set the tokens your instrument uses
(for example `Phase` with `phase, ph`). **Add channel** / **Remove** change
the count; **Save channels** stores the definitions as a new project
revision.

Channels can be changed **only while the project has no datasets**: every
dataset must carry exactly the project's channels. Delete all datasets or
create a new project to change them later. Projects created by earlier
AFMLabel versions are migrated automatically to the two classic channels.

### Editing lock

One browser tab edits a project at a time. The chip in the toolbar shows
`lock: held by this tab`, `lock: free`, or the name of another session.
When another session holds the lock you can take it over (that session
becomes read-only) or open the project read-only yourself. Read-only tabs
can look at everything but change nothing.

## Classes

On the project page add the classes you will annotate: name, colour and
semantic role (`MaterialLayer`, `Boundary`, `Unspecified`). Class identity
is the numeric id and the stable key, not the name or colour; colours are
display only and are also used for the exported colour visualisation.
Rules:

- at most one enabled `Boundary` class per project; it is used by the
  Boundary policy and by Boundary fill (see [Boundary](#boundary));
- a class used by regions cannot be deleted, only disabled;
- **Export id** is the value written into machine-readable masks for the
  class (empty means the class id);
- a class named or keyed `buffer` is proposed automatically by the
  flattened-mask import.

The **Classes** tab of the workspace can add a class quickly; the project
page edits colours, roles and export ids.

## Import images

**Import images** opens the import dialog. It has one file picker per
channel (TIFF, PNG or JPEG) plus an **All channels at once** picker: files
chosen there are assigned to the channel whose filename token they contain
(`sample07_topo.tif` → Topography, `sample07_ph.tif` → Phase). Files that
match no token are listed and skipped; use the per-channel pickers for
them. With a single-channel project every selected file becomes one
dataset. Nothing is written until you confirm the preview.

### Preview the pairing

**Upload and preview pairing** uploads the files and shows one row per
future dataset with one column per channel. Files are grouped by the
identifier left after removing the channel tokens from the filename
(`242_2_topography.tiff` + `242_2_friction.tiff` → dataset `242_2`). A row
can be `OK`, `Missing channel` (the message names the missing channels),
`Ambiguous` (several files of one channel share the key, or no identifier
could be extracted), `Invalid file`, `Dimension mismatch` or `Calibration
mismatch`; only `OK` rows are created. When no identifier can be extracted
at all, the dialog offers pairing by sorted filename order, which you must
confirm explicitly. **Create datasets** commits the `OK` rows; each row
becomes one dataset with one shared annotation grid. All channels of a
dataset must have identical dimensions and compatible calibration;
nothing is ever resampled.

Dataset cards show the thumbnails of the first four channels (a `+N` badge
counts the rest), the original filenames and the region count.

### Replace a source image

**Replace source** on a dataset card uploads a replacement for one channel
and shows a compatibility report (dimensions, calibration against the
other channels, checksum). Only compatible replacements can be confirmed;
annotations are kept, the dataset revision increases and the previous
validation becomes stale. The replacement is recorded in the dataset and
in the audit log.

## Find a dataset (search)

Projects with hundreds of datasets are navigated with the search box above
the dataset cards. It matches, case-insensitively, every original
filename, the pairing key, the dataset name and the dataset id: typing
`242_2` reduces the list to that dataset, `242` to every dataset starting
with it. The count next to the box shows how many cards match; **Clear
filter** shows everything again. The filter is remembered while you open a
dataset and come back.

Inside the workspace, **Previous/Next** (and `PageUp`/`PageDown`) follow
the filtered list while a filter is active and the full list otherwise;
the header shows `filter "242": 2 / 3` so you always know which list you
are walking. The same search exists in the export dialog.

## Annotation workspace

**Annotate** opens the dataset. The workspace has a tool rail on the left,
the canvas in the middle, an action bar with tool options and the active
class under the canvas, a status bar (coverage, missing pixels, overlaps,
validation, host resources) and a right-hand panel with the **Regions**,
**Classes** and **Findings** tabs plus the **View** block.

### Switch channel

All channels of a dataset are views of one field of view, so they share
one annotation: switching changes only the displayed image; regions, zoom,
pan, selection and tool stay. SAM receives the channel that is displayed.

- **1 channel**: the header shows the channel name; there is nothing to
  switch.
- **2 channels**: two buttons (for example **Topography** / **Friction**).
- **3 or more channels**: a channel navigator with previous/next arrows, a
  slider you can drag to scrub through the channels, an editable number
  field (`k / N`) and a list of channel names. The mouse wheel over the
  navigator also steps through the channels.

Keyboard: `1`-`9` show channel 1-9 (`0` shows channel 10), `Tab` shows the
next channel and `Shift+Tab` the previous one (both wrap around). Images
are loaded on demand and cached, so a dataset with many channels opens as
fast as a pair.

### Brightness and contrast (view-only)

The **View** block has **Brightness** and **Contrast** sliders and a
**Reset View** button. They change only how the source image is rendered
on the canvas, before the region overlays are composited, so class
colours, outlines, SAM overlays and Finding markers keep their exact
colours. They are view-only (pixel values, masks and validation are
unchanged), not persisted, not exported and not part of SAM input. Values
are kept per channel for the current page only; switching datasets or
reloading returns to neutral. Double-click a slider to reset that control;
**Reset View** resets both.

## Regions

A *region* is an independent editable annotation object with one class,
one mask, its own id, z-order, visibility, lock state and provenance.
Regions may overlap while you work; the final mask is composed later with
exactly one class per pixel (the highest visible region wins).

### Active class

The active class (Classes tab, or the selector under the canvas) is the
class given to every new region created by brush, polygon, fill or an
accepted SAM proposal. Changing the active class never re-colours or
reclassifies an existing region; use the class selector in a region row for
that.

### Brush rules

The brush (`B`) paints pixels with a round cursor; `[` / `]` change the
radius. The rule is fixed:

```text
selected editable region exists AND selected.class == active class
    -> the stroke extends the selected region

nothing selected
    -> the stroke creates a new region of the active class

selected region exists BUT selected.class != active class
    -> the stroke creates a new region of the active class;
       the previously selected region is not repainted or reclassified

selected region is locked
    -> it is not edited; the stroke creates a new region of the
       active class over it
```

A new region is always allowed on top of existing pixels; the overlap
becomes a Finding. After a stroke creates a region, that region becomes
the selection. The brush cursor shows the colour the stroke will paint
with. The **Eraser** (`E`) removes pixels from the selected region only.

### Polygon

Click vertices (`P`); `Enter` or double-click closes. Plain `Enter`
creates a new region of the active class; `Shift+Enter` adds the polygon
to the selected region, `Alt+Enter` subtracts it (refused on locked
regions). `Backspace` or a right click removes the last vertex.

### Fill tool

The fill tool (`G`) is the fastest way to label areas that are already
enclosed by existing regions, for example grains outlined by a Boundary
class: click inside the empty area and the connected unlabelled pixels
become a new region of the active class. It works in *label space*: the
boundary of the fill is formed by every visible region, whatever the image
looks like, and existing regions are never changed.

While the tool is active the action bar shows its options and a live
preview of the area under the cursor (with its pixel count):

- **Close gaps (px)**: boundaries are thickened by this many pixels before
  the fill is computed, so the fill cannot leak through openings narrower
  than about twice the value; the rim next to the boundary is filled
  afterwards. `0` is an exact flood fill, `1` (default) closes single-pixel
  gaps, higher values close larger openings but also ignore thin unlabelled
  slits of that width.
- **Connectivity**: `4-connected` (default) does not cross diagonal
  corner-to-corner leaks; `8-connected` does.
- **Name for filled regions**: optional name given to every region the
  tool creates (leave empty for unnamed regions).
- **Fill all enclosed gaps**: creates one region of the active class per
  enclosed unlabelled area in a single transaction after you confirm the
  count and the pixel total. Areas touching the image border are skipped
  unless **include areas touching the image border** is ticked.

A fill that would cover more than a quarter of the image is probably not
enclosed; the tool asks for confirmation first. Each single fill is one
undo step; *Fill all* is one undo step for all its regions. Filled regions
record the seed, gap closing and connectivity in their provenance. If you
need the raster coverage of the whole dataset at once, use the Boundary
fill described under [Boundary](#boundary) instead.

### Clear selection

`Esc`, the **Clear selection** rail button or the Regions-panel button
deselect every region and leave the edit context. No region is changed,
reclassified or deleted. The next brush stroke starts a new region of the
active class.

### Selecting regions

| Action | Effect |
| --- | --- |
| Click (row or canvas) | Select one region (primary) |
| `Ctrl+Click` | Toggle a region in the selection |
| `Shift+Click` | Select the contiguous range from the primary region |
| `Ctrl+A` (Regions panel or canvas focused) | Select all listed regions |
| `Esc` | Clear the selection |
| Row checkbox | Same as `Ctrl+Click` |
| **Select All** / **Clear selection** buttons | As above |

Shortcuts never fire while typing in a text field. Every selected region
has an outline on the canvas: white for the primary region, cyan for the
others, orange dashed for a locked one.

### Locked regions

The lock button on a row prevents brush/eraser/polygon edits, class
changes and deletion until the region is unlocked. Locked rows are
striped, carry a `LOCKED` badge and have their edit controls disabled.
Locked regions stay in the composition and may be overlapped by new
regions. **Delete Selected** keeps locked regions and says so; Subtract by
Order refuses when a locked region would lose pixels.

### Z-order

The Regions list shows the highest z-order first: **the top of the list
wins where regions overlap**. Drag a row by its name to reorder.
Reordering changes the composed result but never reviews or removes an
overlap Finding by itself.

### Merge

**Merge checked** merges the selected regions into one editable region.
Only regions of one class can be merged; a mixed selection is refused. The
merged region keeps the primary region's id and records the merged ids in
its provenance. One undo restores every original region exactly.

### Split

**Split** separates the disconnected components of the selected region
into regions of the same class placed directly above it. One undo
restores the exact original region.

### Subtract by Order

Resolves working overlaps *geometrically* according to the current Regions
list order. For the selected regions, from the top of the list down, every
lower region loses the pixels occupied by selected regions above it;
afterwards the selected regions do not overlap one another. Regions that
are not selected are never touched. A preview lists every selected region
with its pixel count before and after and asks for confirmation when a
region would become empty (empty regions are removed). Locked regions that
would lose pixels are refused. One undo restores the exact previous masks.

### Delete Selected

Deletes every selected region (`Delete` key or button) after a
confirmation that lists the total pixel count and any locked regions that
will be kept. One operation is one undo step; undo restores the regions
with their original ids.

### Undo and redo

`Ctrl+Z` / `Ctrl+Y` undo and redo every edit: strokes, region creation,
fills, class/visibility/lock changes, Delete Selected, Merge, Split,
Subtract by Order, z-order changes, Boundary fill, bulk review and imports.
Structural operations are undone by restoring a snapshot of the whole
region set through the server, so ids, masks, classes, order,
lock/visibility, provenance and review state come back exactly. The
restored state is saved as a new revision; a stored validation becomes
stale, never falsely green. Edits autosave; **Saved / Saving…** in the
header shows the state (`Ctrl+S` flushes pending saves).

## SAM proposals

Local segmentation models (Meta SAM 3, SAM 3.1 and SAM 2.1) can propose
masks. They are optional, run only on the AFMLabel host, and never change
the annotation until you accept a proposal. Choose a backend in the
header; the picker states why a backend is unavailable (disabled in the
configuration, checkpoint missing, no CUDA device, load failed) and tools
the backend cannot do stay disabled with the reason in their tooltip.

| Backend | Points | Box | Refine selected (mask prompt) | Text | Device |
| --- | --- | --- | --- | --- | --- |
| `sam3` | yes | yes | yes | yes | CUDA, or CPU when allowed (slow, about 13 s per new image) |
| `sam31` | yes | yes | no | yes | CUDA only |
| `sam21` | yes | yes | yes | no | CUDA or CPU (about 1.4 GiB RAM) |

SAM sees the channel displayed on the canvas, rendered with the same
deterministic percentile stretch the canvas uses at neutral view settings.
Show the channel where the boundary of the structure is clearest
(topography for height steps, friction or phase for material contrast
that is flat in height). Brightness/contrast sliders do not change what
SAM sees.

### Prompts

- **Positive point** (`Q`): a pixel that must belong to the structure. One
  strong point in the middle of a compact structure is the best first
  prompt; `sam3`/`sam21` return up to three variants to pick from.
- **Negative point** (`W`): a pixel that must be excluded.
- **Box** (`X`): drag around the structure; good for elongated or
  low-contrast features. Points can be added after a box.
- **Text prompt** (`sam3`, `sam31`): type a concept and press `Enter`; the
  detector returns every matching instance. Points cannot be combined with
  text.

Points are placed in image coordinates whatever the zoom; zoom and pan
never change the model input. A proposal is drawn as a white overlay;
**Accept as region** creates a region of the active class, **Add to
selected** merges it into the selected region, **Reject** discards it.
Accepted regions record backend, checkpoint hash, code revision, prompts,
channel and source checksum in their provenance; a region edited afterwards
is reported as a corrected model proposal.

### Refining

Tick **Refine selected region** (`sam3`, `sam21`) to send the selected
region as a mask prompt together with your next points or box. Thin
structures may be lost in the model's low-resolution prompt grid: refine
them with points or the brush instead. When a proposal covers two
materials, add a negative point and re-run, or accept it for one class and
paint the other material as a new region on top; the overlap becomes a
Finding to resolve by z-order, review or Subtract by Order.

## Importing annotations

Both importers are two-step (upload and preview, then an explicit commit),
refuse dimension mismatches (nothing is resampled) and record filename,
checksum, dimensions, mapping and importer version in the region
provenance and the audit log.

### XML import (TSWAT / CVAT)

**Import XML annotations** on the project page accepts a *CVAT for images
1.1* XML export. Every XML image is matched to a dataset by the original
filename of any of its channels; a single unmatched image can be assigned
explicitly with **Target dataset**. Every XML label gets a class mapping:
reuse an existing class, create a new one, or do not import those shapes.
Commit creates one region per mask, polygon or box with full XML
provenance. Overlaps are preserved and start **unreviewed**.

### Flattened mask import (lossy)

**Import mask** on a dataset card imports a single-channel label raster
(PNG or TIFF, `0` = unlabelled, every other value a label). A flattened
raster has already lost overlaps and hidden regions, so the imported
regions are marked `lossy_import` and validation keeps warning about
them. The raster must have exactly the dataset's dimensions; colour label
images are refused.

### Default class and value mapping

The preview lists every non-zero value with its pixel and component counts
and a **Target class** selector per value. An enabled class named or keyed
`buffer` is proposed for every value; otherwise the project's configured
default import class; otherwise you must choose explicitly. Discarding a
value is the explicit choice **Do not import this value**, which must be
confirmed with a checkbox. *One region per connected component* (default)
makes every component its own editable region. After the commit the
workspace opens with the imported regions selected; one `Ctrl+Z` removes
the whole import.

## Findings

The **Findings** tab is a live list computed from the current canonical
state: overlaps, missing coverage, hidden regions, class and policy
problems, source problems, lossy imports and corrected model proposals.
Each item has a severity (blocking or warning) and, for overlaps, a review
state. Unreviewed blocking items come first, then unreviewed warnings,
then reviewed items; filters narrow the list by review state, severity and
type. Click an item to navigate to it; `Ctrl+Click`, `Shift+Click` and
`Ctrl+A` select several.

Three kinds of operations stay separate: geometry (brush, fill, Merge,
Split, Subtract by Order, z-order) changes regions; validation stores the
report that gates export; review changes review state only. A Finding that
represents a still-invalid condition (missing coverage, hidden region,
disabled class, policy violation) cannot be waived by review.

### Reviewing overlaps

Marking an overlap reviewed (**Mark Selected Reviewed**, `R`, **Mark All
Visible Reviewed**, or *Resolved by order* / *Intentional* on the item)
acknowledges the current z-order result for those pixels; geometry is never
changed. If the overlapping pixels change later, the overlap becomes
unreviewed again; reordering alone keeps the review.

### Validate

**Validate** (`Ctrl+Enter`, header or Findings panel) composes the final
mask from the current revision, checks every blocking condition
(unreviewed overlaps when review is required, unlabelled pixels, hidden
regions, disabled or missing classes, source problems, Boundary policy)
and stores the report together with the composition hash. A valid report
at the current revision is what export requires. Any later edit makes it
stale: the Findings panel shows a banner and the status bar says `stale`.

### Boundary

A class with the `Boundary` semantic role marks material boundaries. The
Boundary policy (`annotation.boundary_mode`, per project overridable)
decides what happens with unlabelled pixels: `disabled` (Boundary regions
are a violation), `explicit_class` (you annotate them like any class) or
`fill_unlabeled_after_approval`, where the Findings panel offers **Boundary
fill**: after you confirm the exact pixel count, one Boundary region
covering all unlabelled pixels is created at the bottom of the z-order. It
never overwrites a label and is undoable. For filling individual enclosed
areas with a material class use the [fill tool](#fill-tool).

## Export

**Export** on the project page opens the export dialog with the
**NanoUNet** profile preselected. Search the dataset list, keep the
validated datasets ticked, choose the options and **Create export**. The
filename preview shows how the original filenames become the exported
names. Each export is a ZIP; its SHA-256 and size are recorded in the
history with a download link. Export is derived data: it never changes
the annotation.

### Validation requirement

A dataset is exported only when its stored validation report is `valid`,
was computed for the current dataset revision and the current composition
still hashes to the validated one. Anything else is listed as skipped with
a reason. Every exported mask therefore has exactly one value per pixel
and no unreviewed blocking overlap.

### Profiles

| Profile | Layout | Use |
| --- | --- | --- |
| **NanoUNet** (default) | one folder per channel (`Topography/<stem>.png`, `Friction/<stem>.png`, further channels named after their label), `Masks_instance/<stem>.tif` (or `.png`), optional `Masks_color/<stem>.png`, `manifest.json`, `classes.json` | Training data for the NanoUNet loader; for two-channel projects the layout is exactly the loader's |
| Native exact | `datasets/<stem>/final_mask.*`, `instance_mask.*`, `mask_color.png`, untouched source copies of every channel, `regions.json`, `validation.json`, `dataset.json` | Provenance and reproduction of the exact annotation state |

The default profile is `export.default_profile` in `config/app.yaml`.

### Machine-readable mask

`Masks_instance/<stem>.tif` holds one `uint16` scalar per pixel. With
**Mask values = class export id** (default) the value is the class's
export id (falling back to the class id). With **unique id per region
(instance)** each winning region gets its own id numbered in z-order; the
manifest maps ids to regions and classes.

### Boundary policy

Pixels of the `Boundary`-role class are exported as their class/instance
value (`class`, default) or as `0` (`zero`), which the downstream
evaluation ignores. `0` never appears otherwise: unlabelled pixels block
validation.

### PNG and TIFF

**Mask file format** chooses the container of the machine mask:
uncompressed TIFF (`.tif`, the loader's original layout) or PNG. Both hold
identical `uint16` values. Source channel images are always 8-bit
grayscale PNG renders in the NanoUNet profile.

### Colour visualisation

With **Colour visualisation** ticked (default) every sample also gets an
RGB PNG rendered from the same final composition with the exact class
display colours: each pixel is exactly one legend colour, value `0`
renders black, and the legend in `manifest.json` maps every export value to
`class_id`, `stable_key`, name, `display_color` and semantic role. The
colour file is for people; the machine mask stays scalar.

### Class mapping

`manifest.json` and `classes.json` list every class with `class_id`,
`export_id`, `export_value` (the value actually written), `stable_key`,
`name`, `semantic_role`, `enabled` and `display_color`, so the semantics of
every exported pixel are recoverable without the project.

### File naming

Exported sample names come from the original filenames: the pairing key
the importer derived from them (`242_2_topography.tiff` +
`242_2_friction.tiff` → `242_2`) is sanitised to a filesystem-safe stem
and used in every directory of the sample. A key that appears twice gets a
stable `~2`, `~3` suffix. The NanoUNet loader matches files by the
first digit run of the name; when two stems collide under that rule, every
stem of the export gets a zero-padded sequence prefix (`001_242_2`,
`002_242_3`), recorded in the manifest. The manifest records, per dataset,
the pairing key, the original filename of every channel, the export stem,
every written file and the SHA-256 of every source image.

## Administration

- **Configuration**: copy `config/app.example.yaml` to `config/app.yaml`;
  every key can be overridden with `AFMLABEL_<SECTION>__<KEY>` environment
  variables. Important keys: `workspace.root` (all projects live here),
  `server.host` / `server.port`, `security.session_timeout_minutes`,
  `annotation.boundary_mode`, `annotation.require_overlap_review`,
  `export.default_profile`, `models.*` (optional SAM backends).
- **Account**: `afmlabel auth set-password --username lab` creates the
  single account or changes its password (also revokes all sessions);
  `afmlabel auth status`, `afmlabel auth revoke-sessions`.
- **Exposure**: the server speaks plain HTTP; keep `server.host` on
  `127.0.0.1` and reach it through an SSH tunnel or a TLS reverse proxy
  (set `security.secure_cookies: true` behind HTTPS).
- **Backup**: everything is under `workspace.root` (`auth/`, `projects/`,
  `trash/`); copy it with any file-level tool while nobody is editing.
  `cache/` and `tmp/` are disposable. Deleted projects and datasets go to
  `trash/` and can be moved back.
- **Upgrades**: persisted documents carry `schema_version` and are migrated
  on read (projects and datasets written by 0.2.0 become two-channel
  projects). Take a backup before upgrading; a document newer than the
  running version is refused, never downgraded.
- **Models**: optional SAM backends are installed with `pip install -e
  ".[sam]"` after the matching PyTorch build, downloaded with
  `python scripts/download_models.py --model sam3|sam31|sam21` and enabled
  in `config/app.yaml`; `python -m afmlabel.diagnostics.models --backend
  <id>` verifies them. Nothing is downloaded at runtime.

## Keyboard shortcuts

Shortcuts are ignored while typing into a text field; panel-scoped
shortcuts fire only while that panel has focus. On macOS use `Cmd` (⌘)
wherever this guide says `Ctrl` (`Cmd+Click`, `Cmd+Z`, ...); `Ctrl+Click`
opens the context menu there. The table is generated
from `src/afmlabel/static/help/shortcuts.json` by
`scripts/generate_help_reference.py`.

<!-- shortcuts:begin -->
| Keys | Scope | Action |
| --- | --- | --- |
| `Ctrl+Z` | global | Undo the last edit (also Merge, Split, Subtract by Order, Delete Selected) |
| `Ctrl+Y / Ctrl+Shift+Z` | global | Redo |
| `Ctrl+S` | global | Flush pending autosave (edits autosave anyway) |
| `Ctrl+Enter` | global | Validate / revalidate the dataset |
| `Esc` | global | Clear selection, cancel polygon, clear SAM prompts, close popover |
| `V` | global | Select tool |
| `H` | global | Pan tool (Space+drag or middle mouse pans in any tool) |
| `P` | global | Polygon tool |
| `B` | global | Brush tool |
| `E` | global | Eraser tool |
| `G` | global | Fill tool (click an enclosed unlabelled area to create a region of the active class) |
| `[` | global | Decrease brush radius |
| `]` | global | Increase brush radius |
| `Q` | global | SAM positive point (when the backend supports points) |
| `W` | global | SAM negative point |
| `X` | global | SAM box prompt |
| `1 … 9 / 0` | global | Show source channel 1-9 (0 = channel 10) |
| `Tab` | global | Next source channel (wraps around) |
| `Shift+Tab` | global | Previous source channel |
| `F` | global | Fit image to view |
| `+ / =` | global | Zoom in (mouse wheel zooms around the cursor) |
| `-` | global | Zoom out |
| `Enter` | canvas | Polygon: close and create a region (Shift+Enter adds to selected, Alt+Enter subtracts) |
| `Backspace / right click` | canvas | Polygon: remove the last vertex |
| `Ctrl+Click` | regions | Toggle a region in the selection (also on the canvas with the Select tool) |
| `Shift+Click` | regions | Select a contiguous range of regions from the anchor |
| `Ctrl+A` | regions | Select all listed regions |
| `Delete` | global | Delete the selected region(s) after confirmation (locked ones are kept) |
| `Ctrl+Click` | findings | Toggle a Finding in the selection |
| `Shift+Click` | findings | Select a contiguous range of Findings |
| `Ctrl+A` | findings | Select all visible (filtered) Findings |
| `R` | findings | Mark the selected overlap Findings reviewed (review state only) |
| `PageUp` | global | Previous dataset (within the filtered list when a filter is active) |
| `PageDown` | global | Next dataset (within the filtered list when a filter is active) |
<!-- shortcuts:end -->
