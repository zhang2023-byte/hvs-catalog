"""Deterministic one-to-one matching, without semantic inference."""

from collections import deque


def maximum_matching(
    edges: set[tuple[int, int]], left_count: int
) -> list[tuple[int, int]]:
    """Find maximum cardinality with stable traversal and no recursion depth limit."""
    neighbors = [[] for _ in range(left_count)]
    for left, right in sorted(edges):
        neighbors[left].append(right)
    right_to_left = {}
    left_to_right = {}
    for start in range(left_count):
        queue = deque([start])
        seen_left = {start}
        parent = {}
        end = None
        while queue and end is None:
            left = queue.popleft()
            for right in neighbors[left]:
                if right in parent:
                    continue
                parent[right] = left
                owner = right_to_left.get(right)
                if owner is None:
                    end = right
                    break
                if owner not in seen_left:
                    seen_left.add(owner)
                    queue.append(owner)
        if end is None:
            continue
        right = end
        while True:
            left = parent[right]
            previous = left_to_right.get(left)
            right_to_left[right] = left
            left_to_right[left] = right
            if previous is None:
                break
            right = previous
    return sorted(left_to_right.items())
