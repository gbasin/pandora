---
status: log
date: 2026-09-20
---

# Surface cleanup intent

Surface attempts now write a cleanup intent before Docker creates their named
container. Worker-death cleanup removes only a container with the exact attempt
name and complete surface labels, by its inspected immutable ID. Normal runs copy
their container inspection and results before the same cleanup removes the
container, including failed runs. Older stopped surface diagnostics remain
untouched because they have no new cleanup intent.
