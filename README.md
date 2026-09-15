# Recon

Channel SE discovery for [Nutanix Enterprise AI](https://www.nutanix.com/products/nutanix-enterprise-ai). Walk the eight conversations that surface shadow AI, chatbots, RAG, inference, agents, and MCP sprawl — then leave with a targeting report for **private inference** and **Agent Gateway**.

**Live site:** [https://script-repo.github.io/nai-recon/](https://script-repo.github.io/nai-recon/)

## Run locally

```bash
npm install
npm run dev
```

Opens the static app (hash-routed, same UI as Pages). Engagements are stored in the browser only.

```bash
npm run build
```

Writes a static site to `dist-pages/` that you can host anywhere.

## What’s in the app

1. **Start a discovery** — account, industry, Nutanix footprint, core vs deep dive.
2. Walk the eight-stage rail. Capture answers; the coach panel explains why each question exists.
3. Open **Report** for estate map, risks, fit scores, talk track, POC, and objection handling.

**Open sample report** loads Briarhaven Health, a fictional healthcare example.

Optional **Polish targeting memo** needs a server-side xAI key and is disabled on the static Pages build. The rest of the report is generated in-browser without any backend.
