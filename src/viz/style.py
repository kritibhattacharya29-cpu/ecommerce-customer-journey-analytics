"""Shared chart styling.

Two constraints drive every choice here.

**GitHub renders these on either a white or a near-black page**, depending on
the reader's theme, and markdown gives no way to serve different images to each.
A transparent background with dark text vanishes in dark mode. So every figure
gets an explicit light background: in dark mode it reads as a clean white card,
which is legible, rather than as invisible text.

**The palette is colour-blind safe** (blue/orange/grey rather than red/green),
and every series is additionally distinguished by position or label, so no chart
depends on colour alone to be read.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")  # no display on a build machine or in CI

import matplotlib.pyplot as plt

# Colour-blind safe, and distinguishable in greyscale.
BLUE = "#2b6cb0"
ORANGE = "#dd6b20"
GREY = "#a0aec0"
DARK_GREY = "#4a5568"
RED = "#c53030"
GREEN = "#2f855a"
TEXT = "#1a202c"
GRID = "#e2e8f0"
BG = "#ffffff"

FIG_DPI = 150


def apply() -> None:
    plt.rcParams.update({
        "figure.facecolor": BG,
        "axes.facecolor": BG,
        "savefig.facecolor": BG,
        "savefig.bbox": "tight",
        "savefig.dpi": FIG_DPI,
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.titlecolor": TEXT,
        "axes.labelcolor": TEXT,
        "axes.labelsize": 10,
        "axes.edgecolor": GRID,
        "axes.linewidth": 1.0,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "xtick.color": DARK_GREY,
        "ytick.color": DARK_GREY,
        "text.color": TEXT,
        "legend.frameon": False,
        "figure.autolayout": False,
    })


def despine(ax, keep=("left", "bottom")) -> None:
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(side in keep)


def save(fig, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    print(f"    {path.name}")
