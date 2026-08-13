"""The storyboard pipeline: the built-in stages, how a board's edits are layered over them, and the
runner that executes them.

Kept apart from ``api/storyboard.py`` so that the flow is a thing in itself rather than a shape that
emerges from the order of some route handlers.
"""
