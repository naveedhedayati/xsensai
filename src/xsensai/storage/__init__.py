"""Storage layer for x-sensai (Slice 1+).

corpus: iterate cards from disk + load by id
sidecar: byte-exact .raw.txt I/O + sha256 verify
v1_adapter: read-only adapter for v1-shape cards (UC1; deleted in Slice 6)
"""

from xsensai.storage import corpus, sidecar, v1_adapter

__all__ = ["corpus", "sidecar", "v1_adapter"]
