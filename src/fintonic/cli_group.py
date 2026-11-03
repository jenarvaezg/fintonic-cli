"""Typer group that falls back to a default subcommand.

Lets `fintonic products [KIND]` keep working while `fintonic products hide ID` adds
subcommands: anything that is not a known subcommand (or help) goes to `list`.
"""

from __future__ import annotations

from typing import Any

from typer.core import TyperGroup

DEFAULT_COMMAND = "list"


class DefaultCommandGroup(TyperGroup):
    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        help_options = set(ctx.help_option_names)
        if not args or (args[0] not in self.commands and args[0] not in help_options):
            args = [DEFAULT_COMMAND, *args]
        return super().parse_args(ctx, args)
