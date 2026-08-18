---
name: smb-navigation
description: Use to find a shared folder or search indexed file contents with the smallest suitable SMB tool call.
---

# SMB Navigation

Choose the smallest suitable selected tool for the request.

- Use `find_folder` for folder names, locations, and path discovery.
- Use `search_content` for words or facts expected inside indexed files.
- Do not call both tools unless the first result is insufficient and another call remains within the request budget.
- Return the best matches first with short path-oriented explanations.
