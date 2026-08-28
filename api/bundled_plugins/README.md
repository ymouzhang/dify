# Bundled offline plugins

The self-hosted worker image includes pinned, self-contained plugin packages so
air-gapped deployments do not need Marketplace access during workspace setup.
The committed `.difypkg` files are generated artifacts, not the source of truth.

The directory intentionally has only three implementation parts:

- `plugins/`: reviewable plugin source; each plugin's `requirements.txt` is the offline lock and `wheels.sha256` fixes artifacts;
- `packages/`: generated runtime packages and the manifest consumed by Dify.
- `package_plugins.py`: the single build and verification command.

`packages/manifest.json` is the only metadata file. Besides the runtime ID,
version, filename, and digest, it records source digests, target platform, and
Marketplace provenance.

Build the packages on a networked packaging runner, then verify them from the Dify repository root:

```bash
python3 api/bundled_plugins/package_plugins.py build
python3 api/bundled_plugins/package_plugins.py verify
```

The build downloads wheels into a temporary directory and rejects any file not
matching the committed hash lock. It removes the now-invalid Marketplace
signature and remote `uv.lock`, injects the offline dependency configuration,
and writes a stable ZIP with file entries only. Wheels are committed only inside
the final `.difypkg`, not as a second `wheelhouse/` copy. `api/Dockerfile` runs
the offline verifier and never downloads dependencies.

The packages are installed idempotently for existing workspaces by
`bundled_plugin_initializer.py` and for new workspaces by the default-plugin
Celery task. Other configured plugin IDs continue to use Marketplace.

For the background, architecture, operations, upgrade procedure, and
troubleshooting guide, see [DESIGN.zh-CN.md](./DESIGN.zh-CN.md).
