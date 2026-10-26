"""Auto-segmentation of Dad's recordings into musical sections (acts, numbers, movements).

Run with the segmentation venv (librosa lives there, not in the server env):

    nice -n 10 .venv-segment/bin/python -m pipeline.segment.run --ids met-160409-boccanegra

Output: data/sections/<performance_id>.json (schema in docs/sections.md).
"""
VERSION = "seg-0.7"
