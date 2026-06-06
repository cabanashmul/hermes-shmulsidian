# hermes-shmulsidian

A [Hermes Agent](https://hermes-agent.nousresearch.com) memory provider plugin backed by a local [shmulsidian](https://github.com/cabanashmul/shmulsidian) Obsidian vault.

## What It Does

Replaces Hermes's built-in memory system with persistent knowledge stored in your Obsidian vault:

- **System prompt injection** — MEMORY.md vault index is injected into every session
- **Hybrid search** — FTS5 keyword + sqlite-vec semantic search over all vault notes
- **Auto-save sessions** — session summaries saved to `00_Inbox` on session end
- **Memory mirroring** — built-in memory writes are replicated as vault notes
- **4 tools** — `shmulsidian_search`, `shmulsidian_read`, `shmulsidian_create`, `shmulsidian_list`

## Setup

### NixOS (via cabanashmul)

Add as a flake input in `cabanashmul/flake.nix`:

```nix
hermes-shmulsidian.url = "github:shmul95/hermes-shmulsidian";
hermes-shmulsidian.inputs.nixpkgs.follows = "nixpkgs";
```

Import the NixOS module in `nixos/hermes.nix`:

```nix
imports = [
  inputs.hermes-agent.nixosModules.default
  inputs.hermes-shmulsidian.nixosModules.default
];
```

### Manual

```bash
# Copy the plugin
cp -r plugin/ ~/.hermes/plugins/shmulsidian/

# Install Python dependencies
pip install sqlite-vec fastembed

# Set vault path (default: ~/shmulsidian)
export SHMULSIDIAN_VAULT_PATH=~/shmulsidian

# Enable in config
hermes config set memory.provider shmulsidian
```

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `SHMULSIDIAN_VAULT_PATH` | `~/shmulsidian` | Path to the Obsidian vault |

## Requirements

- Python packages: `sqlite-vec`, `fastembed` (for semantic search)
- Falls back to FTS5-only keyword search if sqlite-vec/fastembed are unavailable
- A shmulsidian-structured Obsidian vault (PARA folders, MEMORY.md)

## Tools

| Tool | Description |
|------|-------------|
| `shmulsidian_search` | Hybrid search (semantic + keyword) over vault notes |
| `shmulsidian_read` | Read a note by vault-relative path |
| `shmulsidian_create` | Create a new note with frontmatter |
| `shmulsidian_list` | List notes, optionally filtered by folder |
