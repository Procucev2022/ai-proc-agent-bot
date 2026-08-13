---
name: default
description: Default agent profile with full auto-approval
tools: [read, write, shell]
permissions:
  rules:
    - capability: all
      effect: allow
    - capability: shell
      effect: allow
      match:
        - "*"
    - capability: fs_read
      effect: allow
    - capability: fs_write
      effect: allow
    - capability: mcp
      effect: allow
---

Work on this repository with full command and tool auto-approval.
