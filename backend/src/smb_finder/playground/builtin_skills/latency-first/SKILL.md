---
name: latency-first
description: Use when fast responses matter; minimize tool calls and return a useful partial result before the request time budget expires.
---

# Latency First

Prefer the shortest path to a useful answer.

- Use at most one tool when one result is sufficient.
- Avoid repeating an equivalent search with slightly different wording.
- Keep the final answer compact and surface a useful partial result if the available evidence is incomplete.
- Mention a timeout or unavailable dependency directly instead of spending another call on speculation.
