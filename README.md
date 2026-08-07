# Chad — Personal AI Assistant (Rung 1)

A Telegram bot backed by Claude with an Obsidian vault as memory.

## Quick start

```bash
cd /root/chad
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python -m chad
```

## Architecture

```
Telegram → bot.py → brain.py → vault.py → Obsidian vault (folder of .md files)
```

Each layer only knows about the one below it.

## Tools currently exposed

- `list_notes(folder?)` — list markdown notes in the vault (or a subtree)
- `read_note(note_name)` — read one note
- `append_note(note_name, text)` — append to an existing note
- `create_note(note_name, text)` — create a new note
- `append_to_inbox(text)` — append to today's inbox/YYYY-MM-DD.md
- `edit_section(note_name, section_heading, new_body)` — replace one section body
- `propose_action(kind, args)` — queue a side-effecting action for user approval

`memory.md` is deliberately NOT writable by any tool. The post-turn
extractor (`chad/extractor.py`) is its sole writer.

## Roadmap (future tools)

- `read_document` — extract text from PDFs, docx, pptx inside the vault
- `search_notes` — grep across notes by keyword
- `resolve_link` / `backlinks` — follow and discover Obsidian [[wikilinks]]
- `consolidate_memory` — rewrite memory.md, archive stale content
