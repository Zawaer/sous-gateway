# Valtakirja

Junction 2026 · Valio Aimo challenge **Trusted Trade Agents**.

An open, agent-ready front door for Valio Aimo: an AI purchasing agent carries a signed **mandate** (*valtakirja*) from its human, and Valio Aimo's gateway enforces it. The agent searches the catalog, gets a verifiable quote (price, availability, cut-off, delivery reliability, CO₂, labels) and orders. Anything outside the mandate waits for a human, and every step is logged.

Status: building (13–15 Nov 2026).

## Setup
1. `cp .env.example .env` and fill in the keys (the Valio Aimo API key is on the Junction platform).
2. Product data, delivery reliability and carbon CSVs: download via the API (`/api/products/download` etc.) into `data/` (gitignored).

Start here: [HANDOFF.md](HANDOFF.md) (brief for the agent building this).
