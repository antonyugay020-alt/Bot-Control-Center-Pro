"""Bounded preference for real market publications over subscription writes."""
from collections import deque


class PriorityWaiters:
    def __init__(self, burst=4):
        self.normal = deque()
        self.urgent = deque()
        self.burst = burst
        self.streak = 0

    def __len__(self):
        return len(self.normal) + len(self.urgent)

    def append(self, ticket, urgent=False):
        (self.urgent if urgent else self.normal).append(ticket)

    def remove(self, ticket, urgent=False):
        (self.urgent if urgent else self.normal).remove(ticket)
        if not self.urgent and not self.normal:
            self.streak = 0

    def head(self):
        if self.urgent and (not self.normal or self.streak < self.burst):
            return self.urgent[0]
        return self.normal[0] if self.normal else None

    def admitted(self, urgent):
        # Count only actual grants, not failed or timed-out requests.
        self.streak = min(self.burst, self.streak+1) if urgent else 0
