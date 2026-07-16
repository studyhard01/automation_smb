---
name: skill-creator
description: Use when the user asks to create a new reusable Playground agent skill from a natural-language workflow or repeated task.
---

# Skill Creator

Create one focused, reusable skill that follows the same `<skill-id>/SKILL.md` structure used by the Playground agent.

1. Infer a short skill id from the requested capability. Use only lowercase letters, digits, and hyphens; begin with a letter or digit; keep it under 64 characters.
2. Write a concise description that clearly states both what the skill does and when it should trigger.
3. Write imperative Markdown instructions that give the agent a practical workflow. Keep the body focused and under 500 lines.
4. Call `create_playground_skill` exactly once with `skill_id`, `description`, and `instructions`.
5. Do not include YAML frontmatter in `instructions`; the tool creates and validates the complete SKILL.md document.
6. After the tool succeeds, briefly explain the created skill and state that it is installed and active immediately.

If the requested capability is too ambiguous to produce safe, useful instructions, ask one concise clarification question before calling the tool.
