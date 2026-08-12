"""quickbeam-surgext — a pluggable quickbeam Source turning the Surge XT manual PDF
into a rich Fangorn knowledge graph. Registers the `surgext` source via entry point."""
from .source import SurgeXTSource, build_graph, run

__all__ = ["SurgeXTSource", "build_graph", "run"]
