"""The figure of ``docs/cross-sections-1m.md``: four stored 1 m sections of tile x05y07 with their measures."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import xsec1m as X

INK, INK2, GRID = "#191919", "#666666", "#e6e6e6"
GROUND, BANKFULL, FLOOD = "#257de0", "#f47230", "#00a888"      # categorical slots 1-3 of the reference palette


def pick(sec: pd.DataFrame) -> dict[str, int]:
    """One typical section each: a bedrock slot, a V-shaped creek, a channel with a floodplain and a wide bed."""
    ok = (sec["flags"].to_numpy() & X.DROP) == 0
    s = sec[ok]
    cand = {
        "bedrock slot": s[(s["steep_l"] > 0.8) & (s["steep_r"] > 0.8) & (s["w1"] < 15) & (s["b"] < 0.3)],
        "V-shaped creek": s[(s["b"].between(0.9, 1.1)) & (s["w0"] < 2) & (s["brk"] == 0) & (s["w5"] < 60)],
        "channel with floodplain": s[(s["brk"] == 1) & (s["er"] > 8) & (s["hb_use"].between(1.0, 2.0))
                                     & (s["w0"].between(5, 20))],
        "wide gravel bed": s[(s["w0"] > 60) & (s["w0"] < 200) & (s["brk"] == 1)],
    }
    out = {}
    for k, c in cand.items():
        if len(c):
            med = c["w2"].median()
            out[k] = int((c["w2"] - med).abs().idxmin())
    return out


def figure(dst: Path, tile: str = "x05y07") -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    od = X.out_dir() / "tiles"
    sec = pd.read_parquet(od / f"{tile}_sec.parquet")
    f = np.load(od / f"{tile}_prof.npz")
    P = X.dequantise(f["prof"])
    chosen = pick(sec)
    fig, axes = plt.subplots(2, 2, figsize=(9.6, 6.0), dpi=110)
    plt.rcParams.update({"font.size": 9})
    for ax, (name, i) in zip(axes.ravel(), chosen.items(), strict=False):
        r = sec.loc[i]
        half = 30 if r["w2"] < 25 else (80 if r["w2"] < 120 else 150)
        m = np.abs(X.U) <= half
        u, z = X.U[m], P[i][m]
        hb = float(r["hb_use"])
        top = max(2.0 * hb * 1.45, 4.5)
        ax.plot(u, z, color=GROUND, lw=2, label="ground / water surface (1 m DEM)")
        ax.axhline(hb, color=BANKFULL, lw=1.5, ls="--", label="bankfull stage" if r["brk"] else "5 m (no break)")
        ax.axhline(2 * hb, color=FLOOD, lw=1.5, ls=":", label="twice bankfull (flood-prone width)")
        ax.set_xlim(-half, half)
        ax.set_ylim(-0.5, top)
        ax.grid(color=GRID, lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(INK2)
        ax.tick_params(colors=INK2)
        ax.set_title(name, color=INK, fontsize=10, loc="left")
        steep = np.nanmean([r["steep_l"], r["steep_r"]])
        txt = (f"b = {r['b']:.2f}, A/(wh) at 1 m = {r['ff1']:.2f}\n"
               f"water width {r['w0']:.0f} m, bankfull {hb:.1f} m, ER = {r['er']:.1f}\n"
               f"banks {r['ang_l']:.0f} / {r['ang_r']:.0f} deg, >60 deg share {steep:.2f}")
        ax.text(0.02, 0.97, txt, transform=ax.transAxes, va="top", fontsize=8, color=INK,
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.9, "pad": 1.5})
    for ax in axes[1]:
        ax.set_xlabel("distance from the channel (m)", color=INK2)
    for ax in axes[:, 0]:
        ax.set_ylabel("height above the water surface (m)", color=INK2)
    h, _ = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, ["ground / water surface (1 m DEM)", "bankfull stage (5 m where no break)",
                   "twice the bankfull stage"], loc="lower center", ncol=3, frameon=False, fontsize=8.5)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    dst.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dst)
    plt.close(fig)
    return dst
