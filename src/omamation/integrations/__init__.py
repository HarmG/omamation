"""Integrations: packages of blocks for one service, with their settings in
~/.config/omamation/integrations.json."""

from importlib import import_module

NAMES = ["m365"]


def register(triggers: dict, conditions: dict, actions: dict):
    """Add the blocks of every integration to the registries."""
    for name in NAMES:
        module = import_module(f"{__name__}.{name}")
        for block in getattr(module, "TRIGGERS", []):
            triggers[block.type] = block
        for block in getattr(module, "CONDITIONS", []):
            conditions[block.type] = block
        for block in getattr(module, "ACTIONS", []):
            actions[block.type] = block
