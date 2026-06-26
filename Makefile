.PHONY: sync-agent-docs

sync-agent-docs:
	@echo "Syncing .agent/context.md to tool-specific entry points..."
	@echo "<!-- AUTO-SYNCED from .agent/context.md — do not edit directly. Run: make sync-agent-docs -->" > .github/copilot-instructions.md
	@cat .agent/context.md >> .github/copilot-instructions.md
	@echo "<!-- AUTO-SYNCED from .agent/context.md — do not edit directly. Run: make sync-agent-docs -->" > .cursorrules
	@cat .agent/context.md >> .cursorrules
	@echo "Done. Synced to .github/copilot-instructions.md and .cursorrules"
