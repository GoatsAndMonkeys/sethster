"""Chroma (CENS) + DTW alignment between two recordings of one work.

* CENS: per-frame L1-normalized chroma, quantized, smoothed over ~4 s, resampled to RATE frames/s,
  L2-normalized. Robust to timbre, dynamics and recording quality (Müller 2015, ch. 3).
* DTW: cosine cost computed on the fly in numba, steps (1,1),(1,0),(0,1); free-ish ends (skipping
  frames at either end costs SKIP per frame) so live pre-roll / final applause / cut endings don't
  bend the path. Only the uint8 back-pointer matrix is stored (N*M bytes: 3 h x 3 h at 1 fps = 117 MB).
"""
from __future__ import annotations

import numpy as np
from numba import njit
from scipy.signal import get_window

from .features import FPS

RATE = 1.0          # alignment frames per second
SKIP = 0.25         # cost per skipped frame at the ends (below a typical unrelated-frame cost ~0.3)
MAX_CELLS = 3.2e8   # above this, halve the rate


def cens(chroma: np.ndarray, rate: float = RATE, win_s: float = 4.0) -> np.ndarray:
    """chroma (12 x n frames at FPS) -> (m x 12) CENS at `rate` frames/s."""
    c = np.asarray(chroma, np.float64)
    if c.shape[1] == 0:
        return np.zeros((0, 12))
    c = c / np.maximum(c.sum(0, keepdims=True), 1e-9)
    q = np.zeros_like(c)
    for thr in (0.05, 0.1, 0.2, 0.4):
        q += c > thr
    w = int(round(win_s * FPS)) | 1
    win = get_window("hann", w + 2)[1:-1]
    win /= win.sum()
    sm = np.stack([np.convolve(q[k], win, mode="same") for k in range(12)])
    hop = FPS / rate
    idx = np.clip(np.round(np.arange(0, sm.shape[1], hop)).astype(int), 0, sm.shape[1] - 1)
    out = sm[:, idx].T
    nrm = np.linalg.norm(out, axis=1, keepdims=True)
    flat = np.full((1, 12), 1 / np.sqrt(12))
    out = np.where(nrm > 1e-6, out / np.maximum(nrm, 1e-9), flat)
    return np.ascontiguousarray(out, dtype=np.float32)


@njit(cache=True)
def _dtw(A, B, skip, skip_b):
    n, m = A.shape[0], B.shape[0]
    bp = np.zeros((n, m), np.uint8)       # 0 start, 1 diag, 2 up (i-1), 3 left (j-1)
    prev = np.empty(m, np.float64)
    cur = np.empty(m, np.float64)
    lastcol = np.empty(n, np.float64)
    for i in range(n):
        for j in range(m):
            d = 0.0
            for k in range(A.shape[1]):
                d += A[i, k] * B[j, k]
            c = 1.0 - d
            # starting here costs skip for every frame skipped on either side
            best = c + skip * i + skip_b * j
            arg = 0
            if i > 0 and j > 0 and prev[j - 1] + c < best:
                best = prev[j - 1] + c
                arg = 1
            if i > 0 and prev[j] + c < best:
                best = prev[j] + c
                arg = 2
            if j > 0 and cur[j - 1] + c < best:
                best = cur[j - 1] + c
                arg = 3
            cur[j] = best
            bp[i, j] = arg
        lastcol[i] = cur[m - 1]
        if i == n - 1:
            break
        for j in range(m):
            prev[j] = cur[j]
    # end: anywhere on the last row/column, paying skip for the frames left over
    best = 1e300
    bi, bj = n - 1, m - 1
    for j in range(m):
        v = cur[j] + skip_b * (m - 1 - j)
        if v < best:
            best, bi, bj = v, n - 1, j
    return bp, bi, bj, best, lastcol


@njit(cache=True)
def _backtrack(bp, i, j):
    out = np.empty((i + j + 2, 2), np.int64)
    k = 0
    while True:
        out[k, 0] = i
        out[k, 1] = j
        k += 1
        a = bp[i, j]
        if a == 0:
            break
        if a == 1:
            i -= 1
            j -= 1
        elif a == 2:
            i -= 1
        else:
            j -= 1
    return out[:k][::-1]


def dtw(A: np.ndarray, B: np.ndarray, skip: float = SKIP, skip_b: float | None = None) -> tuple[np.ndarray, float]:
    """Path (k x 2, indices into A and B) and its mean step cost. skip_b=0 makes it a subsequence
    alignment: A is matched somewhere inside B."""
    if len(A) == 0 or len(B) == 0:
        return np.zeros((0, 2), np.int64), 1.0
    skip_b = skip if skip_b is None else skip_b
    bp, bi, bj, best, lastcol = _dtw(A, B, skip, skip_b)
    n = len(A)
    ends = lastcol + skip * (n - 1 - np.arange(n))
    ei = int(np.argmin(ends))
    if ends[ei] < best:
        bi, bj, best = ei, len(B) - 1, float(ends[ei])
    path = _backtrack(bp, bi, bj)
    costs = 1.0 - np.einsum("ij,ij->i", A[path[:, 0]], B[path[:, 1]])
    return path, float(costs.mean())


def baseline_cost(A: np.ndarray, B: np.ndarray, n: int = 4000, seed: int = 0) -> float:
    """Median cost of random frame pairs: what 'unrelated' looks like for these two recordings."""
    rng = np.random.default_rng(seed)
    i = rng.integers(0, len(A), n)
    j = rng.integers(0, len(B), n)
    return float(np.median(1.0 - np.einsum("ij,ij->i", A[i], B[j])))


class Alignment:
    """A DTW path between sequence A (e.g. live) and B (e.g. reference), in seconds."""

    def __init__(self, A: np.ndarray, B: np.ndarray, rate: float | None = None, *, subsequence: bool = False):
        rate = rate or RATE
        if len(A) * len(B) > MAX_CELLS:
            f = int(np.ceil(np.sqrt(len(A) * len(B) / MAX_CELLS)))
            A, B, rate = A[::f], B[::f], rate / f
        self.rate = rate
        self.A, self.B = A, B
        self.path, self.cost = dtw(A, B, skip=1.0 if subsequence else SKIP, skip_b=0.0 if subsequence else None)
        self.baseline = baseline_cost(A, B)
        self.step_cost = 1.0 - np.einsum("ij,ij->i", A[self.path[:, 0]], B[self.path[:, 1]]) \
            if len(self.path) else np.zeros(0)

    @property
    def quality(self) -> float:
        """0 (no better than random pairs) .. 1 (perfect). Good opera alignments score ~0.5-0.8."""
        if self.baseline <= 1e-6:
            return 0.0
        return float(np.clip(1 - self.cost / self.baseline, 0, 1))

    @property
    def coverage(self) -> tuple[float, float]:
        """Fraction of A and of B covered by the path."""
        if not len(self.path):
            return 0.0, 0.0
        a = (self.path[-1, 0] - self.path[0, 0] + 1) / max(len(self.A), 1)
        b = (self.path[-1, 1] - self.path[0, 1] + 1) / max(len(self.B), 1)
        return float(a), float(b)

    def b_to_a(self, tb: float) -> tuple[float, float]:
        """Map a time in B to A. Returns (time in A, local quality 0..1 around it)."""
        jb = tb * self.rate
        p = self.path
        k = int(np.searchsorted(p[:, 1], jb, side="left"))
        k = min(max(k, 0), len(p) - 1)
        ia = float(p[k, 0])
        if k > 0 and p[k, 1] != p[k - 1, 1]:
            # interpolate within the step for sub-frame precision
            j0, j1 = p[k - 1, 1], p[k, 1]
            frac = (jb - j0) / max(j1 - j0, 1e-9)
            ia = p[k - 1, 0] + frac * (p[k, 0] - p[k - 1, 0])
        return ia / self.rate, self.local_quality(k)

    def b_span(self, tb: float) -> tuple[float, float, float]:
        """Live times around a boundary tb in B: (end of what precedes it, start of what follows it,
        local quality). The two differ when A has extra material there (applause, a pause, talk)."""
        jb = int(round(tb * self.rate))
        p = self.path
        k_next = int(np.searchsorted(p[:, 1], jb, side="left"))
        k_next = min(max(k_next, 0), len(p) - 1)
        k_prev = int(np.searchsorted(p[:, 1], jb - 1, side="right")) - 1
        k_prev = min(max(k_prev, 0), len(p) - 1)
        start_next = p[k_next, 0] / self.rate
        end_prev = (p[k_prev, 0] + 1) / self.rate if jb > p[0, 1] else start_next
        return min(end_prev, start_next), start_next, self.local_quality(k_next)

    def a_to_b(self, ta: float) -> tuple[float, float]:
        ia = ta * self.rate
        p = self.path
        k = int(np.searchsorted(p[:, 0], ia, side="left"))
        k = min(max(k, 0), len(p) - 1)
        jb = float(p[k, 1])
        if k > 0 and p[k, 0] != p[k - 1, 0]:
            i0, i1 = p[k - 1, 0], p[k, 0]
            frac = (ia - i0) / max(i1 - i0, 1e-9)
            jb = p[k - 1, 1] + frac * (p[k, 1] - p[k - 1, 1])
        return jb / self.rate, self.local_quality(k)

    def local_quality(self, k: int, half_s: float = 12.0) -> float:
        h = max(2, int(half_s * self.rate))
        seg = self.step_cost[max(0, k - h): k + h + 1]
        if not len(seg) or self.baseline <= 1e-6:
            return 0.0
        q = 1 - float(np.median(seg)) / self.baseline
        # penalize paths that run flat (one side frozen) around the point: insertions / cuts
        p = self.path[max(0, k - h): k + h + 1]
        da = p[-1, 0] - p[0, 0]
        db = p[-1, 1] - p[0, 1]
        slope = (da + 1) / (db + 1)
        if not 0.5 <= slope <= 2.0:
            q *= 0.6
        return float(np.clip(q, 0, 1))

    def b_extent(self, a0: float, a1: float) -> float:
        """Seconds of B the path covers while A runs over [a0, a1)."""
        p = self.path
        m = (p[:, 0] >= a0 * self.rate) & (p[:, 0] < a1 * self.rate)
        if not m.any():
            return 0.0
        return float(p[m, 1].max() - p[m, 1].min()) / self.rate

    def quality_in(self, a0: float, a1: float) -> float | None:
        """Match quality of the path over A-times [a0, a1) (None if the path doesn't cover it)."""
        p = self.path
        m = (p[:, 0] >= a0 * self.rate) & (p[:, 0] < a1 * self.rate)
        if not m.any() or self.baseline <= 1e-6:
            return None
        return float(np.clip(1 - float(np.median(self.step_cost[m])) / self.baseline, 0, 1))

    def sample_points(self, step_s: float = 2.0) -> list[list[float]]:
        """[[tA, tB], ...] every ~step_s along the path (monotone), for storage."""
        out: list[list[float]] = []
        last_a = -1e9
        for ia, jb in self.path:
            ta, tb = ia / self.rate, jb / self.rate
            if ta - last_a >= step_s:
                out.append([round(ta, 2), round(tb, 2)])
                last_a = ta
        if len(self.path):
            ia, jb = self.path[-1]
            out.append([round(ia / self.rate, 2), round(jb / self.rate, 2)])
        return out
