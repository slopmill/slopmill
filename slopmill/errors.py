# SPDX-License-Identifier: MIT
"""One error type for everything the author can fix in the source or the pack."""


class Problem:
    __slots__ = ("line", "message")

    def __init__(self, message, line=None):
        self.message = message
        self.line = line

    def __str__(self):
        return f"line {self.line}: {self.message}" if self.line else self.message


class CompileError(Exception):
    """Raised with every problem found, not just the first, so one run lists them all."""

    def __init__(self, problems, path=None):
        self.problems = list(problems)
        self.path = path
        super().__init__(self.render())

    def render(self):
        head = f"{self.path}: " if self.path else ""
        n = len(self.problems)
        lines = [f"{head}{n} problem{'s' if n != 1 else ''}"]
        lines += [f"  - {p}" for p in self.problems]
        return "\n".join(lines)


class EnvironmentProblem(Exception):
    """Pandoc missing, wrong version, unreadable pack. Not the author's fault."""
