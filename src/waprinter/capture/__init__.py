"""Getting a print job off the Windows spooler and into the pipeline."""

from .watcher import SpoolWatcher, claim, is_complete

__all__ = ["SpoolWatcher", "claim", "is_complete"]
