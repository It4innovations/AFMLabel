// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Full-resolution mask utilities. A mask is a Uint8Array of width*height
// bytes (0/1), row-major, matching the source image grid exactly.

export function decodeRows(counts, width, height) {
  const total = width * height;
  const mask = new Uint8Array(total);
  let position = 0;
  let value = 0;
  for (const run of counts) {
    if (value) mask.fill(1, position, position + run);
    position += run;
    value ^= 1;
  }
  if (position !== total) throw new Error(`run lengths sum to ${position}, expected ${total}`);
  return mask;
}

export function encodeRows(mask) {
  const counts = [];
  let value = 0;
  let run = 0;
  for (let i = 0; i < mask.length; i += 1) {
    const v = mask[i] ? 1 : 0;
    if (v === value) {
      run += 1;
    } else {
      counts.push(run);
      value = v;
      run = 1;
    }
  }
  counts.push(run);
  return counts;
}

export function maskArea(mask) {
  let area = 0;
  for (let i = 0; i < mask.length; i += 1) area += mask[i];
  return area;
}

export function maskBounds(mask, width, height) {
  let minX = width, minY = height, maxX = -1, maxY = -1;
  for (let y = 0; y < height; y += 1) {
    const row = y * width;
    for (let x = 0; x < width; x += 1) {
      if (mask[row + x]) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return null;
  return { x: minX, y: minY, w: maxX - minX + 1, h: maxY - minY + 1 };
}

export function paintDisk(mask, width, height, cx, cy, radius, value) {
  const r2 = radius * radius;
  const y0 = Math.max(0, Math.floor(cy - radius));
  const y1 = Math.min(height - 1, Math.ceil(cy + radius));
  const x0 = Math.max(0, Math.floor(cx - radius));
  const x1 = Math.min(width - 1, Math.ceil(cx + radius));
  for (let y = y0; y <= y1; y += 1) {
    const dy = y + 0.5 - cy;
    for (let x = x0; x <= x1; x += 1) {
      const dx = x + 0.5 - cx;
      if (dx * dx + dy * dy <= r2) mask[y * width + x] = value;
    }
  }
}

export function paintStroke(mask, width, height, x0, y0, x1, y1, radius, value) {
  const distance = Math.hypot(x1 - x0, y1 - y0);
  const step = Math.max(0.5, radius / 2);
  const steps = Math.max(1, Math.ceil(distance / step));
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    paintDisk(mask, width, height, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, radius, value);
  }
}

export function rasterizePolygon(points, width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  ctx.fillStyle = "#fff";
  ctx.beginPath();
  points.forEach(([x, y], index) => (index ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
  ctx.fill();
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#fff";
  ctx.stroke();
  const data = ctx.getImageData(0, 0, width, height).data;
  const mask = new Uint8Array(width * height);
  for (let i = 0; i < mask.length; i += 1) mask[i] = data[i * 4 + 3] > 127 ? 1 : 0;
  return mask;
}

// Multiplicity of visible regions per pixel plus derived coverage figures.
export function coverageStats(masks, width, height) {
  const counts = new Uint8Array(width * height);
  for (const mask of masks) {
    for (let i = 0; i < counts.length; i += 1) {
      if (mask[i] && counts[i] < 255) counts[i] += 1;
    }
  }
  let labeled = 0;
  let overlapped = 0;
  let maxMultiplicity = 0;
  for (let i = 0; i < counts.length; i += 1) {
    const c = counts[i];
    if (c) labeled += 1;
    if (c > 1) overlapped += 1;
    if (c > maxMultiplicity) maxMultiplicity = c;
  }
  return { counts, total: counts.length, labeled, missing: counts.length - labeled, overlapped, maxMultiplicity };
}

// Outline (edge) pixels of a mask, for the selected-region highlight.
export function maskEdges(mask, width, height) {
  const edges = new Uint8Array(width * height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const i = y * width + x;
      if (!mask[i]) continue;
      if (
        x === 0 || y === 0 || x === width - 1 || y === height - 1 ||
        !mask[i - 1] || !mask[i + 1] || !mask[i - width] || !mask[i + width]
      ) edges[i] = 1;
    }
  }
  return edges;
}

// ---- seed fill (fill tool) ---------------------------------------------------
// All helpers work in *label space*: `counts` is the per-pixel multiplicity of
// visible regions (coverageStats().counts); a pixel is "free" when no region
// covers it. Filling never touches a labelled pixel.

// Connected component of free pixels (free[i] = 1) containing (sx, sy), or
// null when the seed itself is not free. Iterative, no recursion.
export function floodFillComponent(free, width, height, sx, sy, connectivity = 4) {
  const x0 = Math.floor(sx);
  const y0 = Math.floor(sy);
  if (x0 < 0 || y0 < 0 || x0 >= width || y0 >= height) return null;
  const seed = y0 * width + x0;
  if (!free[seed]) return null;
  const out = new Uint8Array(width * height);
  const stack = new Int32Array(width * height);
  let top = 0;
  stack[top++] = seed;
  out[seed] = 1;
  const eight = connectivity === 8;
  while (top) {
    const i = stack[--top];
    const x = i % width;
    const y = (i - x) / width;
    const left = x > 0, right = x < width - 1, up = y > 0, down = y < height - 1;
    if (left && free[i - 1] && !out[i - 1]) { out[i - 1] = 1; stack[top++] = i - 1; }
    if (right && free[i + 1] && !out[i + 1]) { out[i + 1] = 1; stack[top++] = i + 1; }
    if (up && free[i - width] && !out[i - width]) { out[i - width] = 1; stack[top++] = i - width; }
    if (down && free[i + width] && !out[i + width]) { out[i + width] = 1; stack[top++] = i + width; }
    if (eight) {
      if (up && left && free[i - width - 1] && !out[i - width - 1]) { out[i - width - 1] = 1; stack[top++] = i - width - 1; }
      if (up && right && free[i - width + 1] && !out[i - width + 1]) { out[i - width + 1] = 1; stack[top++] = i - width + 1; }
      if (down && left && free[i + width - 1] && !out[i + width - 1]) { out[i + width - 1] = 1; stack[top++] = i + width - 1; }
      if (down && right && free[i + width + 1] && !out[i + width + 1]) { out[i + width + 1] = 1; stack[top++] = i + width + 1; }
    }
  }
  return out;
}

// Square (chessboard) binary dilation by `radius` pixels via separable
// running sums: O(width*height) regardless of the radius.
export function dilate(mask, width, height, radius) {
  if (radius <= 0) return mask;
  const tmp = new Uint8Array(width * height);
  const out = new Uint8Array(width * height);
  const prefix = new Int32Array(Math.max(width, height) + 1);
  for (let y = 0; y < height; y += 1) {
    const row = y * width;
    prefix[0] = 0;
    for (let x = 0; x < width; x += 1) prefix[x + 1] = prefix[x] + mask[row + x];
    for (let x = 0; x < width; x += 1) {
      const a = Math.max(0, x - radius);
      const b = Math.min(width, x + radius + 1);
      tmp[row + x] = prefix[b] - prefix[a] > 0 ? 1 : 0;
    }
  }
  for (let x = 0; x < width; x += 1) {
    prefix[0] = 0;
    for (let y = 0; y < height; y += 1) prefix[y + 1] = prefix[y] + tmp[y * width + x];
    for (let y = 0; y < height; y += 1) {
      const a = Math.max(0, y - radius);
      const b = Math.min(height, y + radius + 1);
      out[y * width + x] = prefix[b] - prefix[a] > 0 ? 1 : 0;
    }
  }
  return out;
}

// Grow `labels` (Int32Array, 0 = none) into neighbouring free pixels for
// `iterations` steps (geodesic dilation). Used to give filled components the
// rim that gap closing removed, without crossing labelled pixels.
//
// Callers that undo a dilate() must pass connectivity 8: dilate() uses the
// chessboard metric, so a pixel next to a diagonal wall or in a concave
// staircase corner is up to 2*radius 4-steps away from the thinned area but
// at most `radius` 8-steps. With 4-steps those pixels stayed unfilled. The
// 8-step growth cannot cross a corner-to-corner leak either: every pixel of
// the (2r+1)^2 square around a thinned pixel is free, so the growth stays in
// the 4-connected free component the thinned area belongs to.
export function propagateLabels(labels, free, width, height, iterations, connectivity = 4) {
  let current = labels;
  const eight = connectivity === 8;
  for (let step = 0; step < iterations; step += 1) {
    const next = current.slice();
    let changed = false;
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const i = y * width + x;
        if (current[i] || !free[i]) continue;
        let label = 0;
        if (x > 0 && current[i - 1]) label = current[i - 1];
        else if (x < width - 1 && current[i + 1]) label = current[i + 1];
        else if (y > 0 && current[i - width]) label = current[i - width];
        else if (y < height - 1 && current[i + width]) label = current[i + width];
        else if (eight) {
          if (x > 0 && y > 0 && current[i - width - 1]) label = current[i - width - 1];
          else if (x < width - 1 && y > 0 && current[i - width + 1]) label = current[i - width + 1];
          else if (x > 0 && y < height - 1 && current[i + width - 1]) label = current[i + width - 1];
          else if (x < width - 1 && y < height - 1 && current[i + width + 1]) label = current[i + width + 1];
        }
        if (label) { next[i] = label; changed = true; }
      }
    }
    current = next;
    if (!changed) break;
  }
  return current;
}

function freeMasks(counts) {
  const free = new Uint8Array(counts.length);
  const labeled = new Uint8Array(counts.length);
  for (let i = 0; i < counts.length; i += 1) {
    if (counts[i]) labeled[i] = 1; else free[i] = 1;
  }
  return { free, labeled };
}

// Fill the enclosed unlabelled area around a seed. With `gapPx > 0` the
// labelled pixels are first thickened by gapPx so the fill cannot leak
// through boundary gaps narrower than ~2*gapPx; the result is then grown
// back (within free pixels only) so the rim next to the boundary is filled.
// Returns {mask, area, gapClosed} or null when the seed is on a labelled pixel.
export function fillFromSeed(counts, width, height, sx, sy, { gapPx = 0, connectivity = 4 } = {}) {
  const { free, labeled } = freeMasks(counts);
  let component = null;
  let gapClosed = false;
  if (gapPx > 0) {
    const blocked = dilate(labeled, width, height, gapPx);
    const narrow = new Uint8Array(free.length);
    for (let i = 0; i < free.length; i += 1) narrow[i] = free[i] && !blocked[i] ? 1 : 0;
    component = floodFillComponent(narrow, width, height, sx, sy, connectivity);
    if (component) {
      gapClosed = true;
      const labels = new Int32Array(free.length);
      for (let i = 0; i < free.length; i += 1) if (component[i]) labels[i] = 1;
      const grown = propagateLabels(labels, free, width, height, gapPx, 8);
      for (let i = 0; i < free.length; i += 1) component[i] = grown[i] ? 1 : 0;
    }
  }
  if (!component) component = floodFillComponent(free, width, height, sx, sy, connectivity);
  if (!component) return null;
  return { mask: component, area: maskArea(component), gapClosed };
}

// Label every free component (after optional gap closing). Returns
// {labels, count, sizes, touchesBorder}; masks are built on demand with
// componentMask() so thousands of small gaps do not need a mask each.
export function freeComponents(counts, width, height, { gapPx = 0, connectivity = 4 } = {}) {
  const { free, labeled } = freeMasks(counts);
  let seedable = free;
  if (gapPx > 0) {
    const blocked = dilate(labeled, width, height, gapPx);
    seedable = new Uint8Array(free.length);
    for (let i = 0; i < free.length; i += 1) seedable[i] = free[i] && !blocked[i] ? 1 : 0;
  }
  let labels = new Int32Array(free.length);
  const stack = new Int32Array(free.length);
  const eight = connectivity === 8;
  let count = 0;
  for (let start = 0; start < free.length; start += 1) {
    if (!seedable[start] || labels[start]) continue;
    count += 1;
    let top = 0;
    stack[top++] = start;
    labels[start] = count;
    while (top) {
      const i = stack[--top];
      const x = i % width;
      const y = (i - x) / width;
      const nbrs = [];
      if (x > 0) nbrs.push(i - 1);
      if (x < width - 1) nbrs.push(i + 1);
      if (y > 0) nbrs.push(i - width);
      if (y < height - 1) nbrs.push(i + width);
      if (eight) {
        if (x > 0 && y > 0) nbrs.push(i - width - 1);
        if (x < width - 1 && y > 0) nbrs.push(i - width + 1);
        if (x > 0 && y < height - 1) nbrs.push(i + width - 1);
        if (x < width - 1 && y < height - 1) nbrs.push(i + width + 1);
      }
      for (const j of nbrs) {
        if (seedable[j] && !labels[j]) { labels[j] = count; stack[top++] = j; }
      }
    }
  }
  if (gapPx > 0 && count) labels = propagateLabels(labels, free, width, height, gapPx, 8);
  const sizes = new Int32Array(count + 1);
  const touchesBorder = new Uint8Array(count + 1);
  for (let i = 0; i < labels.length; i += 1) {
    const label = labels[i];
    if (!label) continue;
    sizes[label] += 1;
    const x = i % width;
    const y = (i - x) / width;
    if (x === 0 || y === 0 || x === width - 1 || y === height - 1) touchesBorder[label] = 1;
  }
  return { labels, count, sizes, touchesBorder };
}

export function componentMask(labels, label) {
  const mask = new Uint8Array(labels.length);
  for (let i = 0; i < labels.length; i += 1) if (labels[i] === label) mask[i] = 1;
  return mask;
}

export function hexToRgb(hex) {
  const value = parseInt(hex.replace("#", ""), 16);
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}
