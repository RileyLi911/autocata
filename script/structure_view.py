"""Geometry-only preparation shared by raster previews and the HTML gallery.

Coordinates are display copies. Periodic image changes are lattice translations;
the final change of basis is a proper rotation, never a reflection.
"""
from collections import Counter

import numpy as np
from ase.formula import Formula
from ase.geometry import find_mic
from ase.io import read


def adsorbate_mask(atoms, row):
    # OC20 tags explicitly identify adsorbates. Otherwise use AutoCata's
    # validated trailing-formula convention, not element identity (oxide slabs).
    tags = atoms.get_tags()
    if np.any(tags == 2) and np.any(tags != 2):
        return tags == 2
    formula = next((row.get(k) for k in (
        'adsorbate_formula', 'adsorbate_expected', '_adsorbate', 'adsorbate'
    ) if row.get(k)), None)
    if not formula:
        raise ValueError('Adsorbate identification requires OC20 tags or an adsorbate formula.')
    counts = Formula(str(formula)).count()
    n = sum(counts.values())
    if not 0 < n < len(atoms) or Counter(atoms.get_chemical_symbols()[-n:]) != counts:
        raise ValueError('Trailing atoms do not match the adsorbate formula or slab is missing.')
    return np.arange(len(atoms)) >= len(atoms) - n


def prepare_view(atoms, row):
    mask = adsorbate_mask(atoms, row)
    pos = atoms.positions.copy()
    cell = np.asarray(atoms.cell)
    periodic = atoms.pbc
    normal = None
    if atoms.cell.rank == 3:
        frac = atoms.get_scaled_positions(wrap=False)
        reciprocal = np.linalg.inv(cell)
        candidates = []
        for axis in range(3):
            direction = reciprocal[:, axis]
            spacing = 1.0 / np.linalg.norm(direction)
            values = np.sort(frac[~mask, axis] % 1.0)
            gaps = np.diff(np.r_[values, values[0] + 1.0])
            gap_index = int(np.argmax(gaps))
            candidates.append((gaps[gap_index] * spacing, axis,
                               (values[gap_index] + gaps[gap_index] / 2) % 1.0))
        # For a 2D-periodic slab the nonperiodic cell direction is definitive.
        choices = [c for c in candidates if not periodic[c[1]]] if sum(periodic) == 2 else candidates
        _, axis, cut = max(choices)
        normal = reciprocal[:, axis] / np.linalg.norm(reciprocal[:, axis])
        if periodic[axis]:
            shifts = np.floor(frac[~mask, axis] - cut)
            pos[~mask] -= shifts[:, None] * cell[axis]

    if normal is None:
        slab = pos[~mask] - pos[~mask].mean(axis=0)
        if len(slab) < 3 or np.linalg.matrix_rank(slab) < 2:
            raise ValueError('Insufficient slab geometry to determine a surface normal.')
        _, _, axes = np.linalg.svd(slab, full_matrices=False)
        normal = axes[-1]

    if np.any(periodic):
        # Minimum-image spanning tree makes a boundary-split molecule whole.
        indices = np.flatnonzero(mask)
        done, remaining = [int(indices[0])], set(map(int, indices[1:]))
        while remaining:
            pairs = [(i, j) for i in done for j in sorted(remaining)]
            vectors, lengths = find_mic(np.array([atoms.positions[j] - atoms.positions[i]
                                                  for i, j in pairs]), cell, periodic)
            k = int(np.argmin(lengths))
            i, j = pairs[k]
            pos[j] = pos[i] + vectors[k]
            done.append(j)
            remaining.remove(j)
        # Select the molecular image adjacent to its nearest slab atom.
        pairs = [(i, j) for i in np.flatnonzero(~mask) for j in indices]
        vectors, lengths = find_mic(np.array([pos[j] - pos[i] for i, j in pairs]), cell, periodic)
        k = int(np.argmin(lengths))
        i, j = pairs[k]
        pos[mask] += pos[i] + vectors[k] - pos[j]

    separation = (pos[mask].mean(axis=0) - pos[~mask].mean(axis=0)) @ normal
    if abs(separation) < 1e-8:
        raise ValueError('Adsorbate side is ambiguous (centered in the slab).')
    normal *= np.sign(separation)
    reference = np.eye(3)[np.argmin(abs(normal))]
    x = reference - normal * (reference @ normal)
    x /= np.linalg.norm(x)
    basis = np.column_stack((x, np.cross(normal, x), normal))
    oriented = (pos - pos.mean(axis=0)) @ basis
    result = atoms.copy()
    result.positions = oriented
    result.set_cell(cell @ basis)
    return result, mask


def load_view(path, row):
    # ASE auto-detects extended XYZ, preserving Lattice, pbc and tags.
    return prepare_view(read(path), row)


def project_view(positions, mask, azimuth_deg):
    """Rotate about the oriented surface normal; +normal projects screen-up.

    Keep the original 58 degree oblique style where possible. Reduce perspective
    overlap for off-center adsorbates using one conservative tilt for all frames.
    """
    normal_gap = positions[mask, 2].min() - positions[~mask, 2].max()
    if normal_gap > 1e-8:
        lateral = (np.linalg.norm(positions[mask, :2], axis=1).max()
                   + np.linalg.norm(positions[~mask, :2], axis=1).max())
    else:
        delta = positions[mask].mean(axis=0) - positions[~mask].mean(axis=0)
        normal_gap, lateral = delta[2], np.linalg.norm(delta[:2])
    tilt = max(np.radians(58.0), np.arctan2(lateral * 1.01, normal_gap))
    a = np.radians(azimuth_deg)
    rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    # Negative x rotation sends +z to +y; the raster canvas subsequently inverts y.
    rx = np.array([[1, 0, 0], [0, np.cos(tilt), np.sin(tilt)], [0, -np.sin(tilt), np.cos(tilt)]])
    return positions @ rz.T @ rx.T
