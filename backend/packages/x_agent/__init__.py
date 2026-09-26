"""Shared X Agent analysis primitives.

The package has no dependency on the main X newsflash pipeline.  Its caller
owns collection and persistence, while this package only decides whether an
X post is worth sending to the low-cost structured extractor.
"""

from .analysis import XAgentAnalyzer, XAgentModelFailure, is_relevant

__all__ = ["XAgentAnalyzer", "XAgentModelFailure", "is_relevant"]
