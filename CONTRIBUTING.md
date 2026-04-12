# Contributing to AgentMesh

Thanks for your interest in contributing. AgentMesh is in early design phase — the best contributions right now are:

1. **Protocol feedback** — review the [AMP event spec](docs/design.md) and open an issue with suggestions
2. **Framework adapter stubs** — skeleton implementations for Claude SDK, LangGraph, CrewAI, OpenAI Agents SDK
3. **Dashboard designs** — mockups for the real-time agent graph view
4. **SDK API feedback** — is `@mesh.agent` the right abstraction? Open a discussion

## How to contribute

1. Fork the repo
2. Open an issue first for any non-trivial change
3. Submit a PR referencing the issue

## Design principles

- **Zero friction first** — `pip install agentmesh` + one decorator = working dashboard
- **Protocol open** — AMP spec is public so any framework can implement natively
- **OSS-first** — cloud product is additive, never extractive from the OSS version

## Related projects

- [mission-control-openclaw](https://github.com/hansraj316/mission-control-openclaw) — a reference implementation of an agent dashboard built with Python + SQLite (battle-tested with 25 agents)
- [agentdate](https://github.com/hansraj316/agentdate) — agent discovery and registration platform

## License

Apache 2.0
