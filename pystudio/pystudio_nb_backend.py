"""matplotlib backend for PyStudio notebooks (MPLBACKEND=module://pystudio_nb_backend): figures draw with Agg and
plt.show() puts them under the cell, where it's called - like Jupyter's inline backend."""
import sys

from matplotlib.backend_bases import FigureManagerBase
from matplotlib.backends.backend_agg import FigureCanvasAgg

FigureCanvas = FigureCanvasAgg
FigureManager = FigureManagerBase


def show(*_args, **_kwargs):
    kernel = sys.modules.get("__main__")
    flush = getattr(kernel, "_flush_figures", None)
    if flush is not None:
        flush()
