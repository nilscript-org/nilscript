# Known Implementations

NIL is implementation-independent; this registry lists implementations with published
conformance reports (see
[versions/0.1.0-conformance-checklist.md](versions/0.1.0-conformance-checklist.md)).

| Name | Role | Conformance target | Notes |
|---|---|---|---|
| **wosool** | Hosted System (reference) | **NIL-H** (Core §8 + H1–H8), MCP + HTTP bindings | The standard's steward; implementation report to be published with the first non-draft release |
| **any MCP client** (Claude, ChatGPT, …) | Speaker | §4–§7 via the MCP binding | No NIL-specific code required beyond the tools |
| **pocketbase-nil-adapter** 🟢 Official Verified | Adapter (System shim) | offline 16/16 + manifest validate (live gate opt-in); conforms to `nilscript>=0.3.0` | Standalone repo: [nilscript-org/pocketbase-nil-adapter](https://github.com/nilscript-org/pocketbase-nil-adapter). Reference adapter for [PocketBase](https://pocketbase.io/); also the canonical in-core example under [`examples/pocketbase-adapter/`](examples/pocketbase-adapter/). |

To be listed: open a PR adding your implementation with a completed implementation report
(checklist statuses + the `examples/` exchanges executed against your System, logs attached).
The 1.0 release requires two independent interoperable implementations.
