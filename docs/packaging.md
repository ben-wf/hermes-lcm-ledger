# Installing LCM Ledger

Hermes loads the directory plugin through `plugin.yaml` and
`register(ctx)` in the adjacent `__init__.py`. The metadata-only
`pyproject.toml` lets the Hermes package manager resolve the checkout.
The core archive and full-text search use SQLite. Optional retrieval
dependencies and the supported host range are documented in
[the dependency contract](../dependency-contract.json).

From the Ledger checkout, install into the active Hermes profile:

```bash
hermes plugins install "file://$(pwd -P)" --ref "$(git rev-parse HEAD)"
```

For development, `./scripts/install.sh` links this checkout and its skill into
the selected profile. Use just one installation method per profile.

When switching from Hermes-LCM, stop all writers and take a consistent backup
of the profile database and its external payload directory. Replace
`hermes-lcm` in `plugins.enabled` with `hermes-lcm-ledger`. Carry over any
plugin-specific settings and update monitoring that checks the plugin name.
Keep `context.engine: lcm`, the existing `lcm:` settings, database and payload
paths. Enable only one LCM implementation in a profile. To roll back, stop
writers, restore the previous config and plugin, and restore the backup if the
archive was damaged. Restoring an old snapshot discards later turns.
