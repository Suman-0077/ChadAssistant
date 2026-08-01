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

## Roadmap (future tools)

- `read_document` — extract text from PDFs, docx, pptx inside the vault
- `search_notes` — grep across notes by keyword
- `resolve_link` / `backlinks` — follow and discover Obsidian [[wikilinks]]
- `edit_section` — targeted in-place editing (not just append)
