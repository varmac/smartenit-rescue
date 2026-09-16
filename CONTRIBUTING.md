# Contributing to Smartenit Rescue

Thank you for helping make Smartenit Rescue safer and more useful.

## Required practices

- Every contribution must include a `Signed-off-by` line in commit messages
  to satisfy Developer Certificate of Origin (DCO) expectations.
- New fixtures must be synthetic and must not contain real hostnames, private
  addresses, device identifiers, credentials, paths, or captured deployment
  data.
- Evidence for support claims must include provenance metadata and remain
  accurate to the level of public verification.
- Harmony G2 contributions must preserve the LAN-only, certificate-pinned transport,
  externally provisioned local-session boundary, exact EUI-64/component mapping, and
  command-only state semantics documented in `docs/harmony-g2.md`.
- Never contribute a real session, certificate fingerprint, gateway address, component
  ID, device identifier, inventory response, household name, or private filesystem
  path. Real-device evidence must be reduced to a sanitized checklist result before it
  enters the public tree.
- A model/capability support row may move to `hardware_verified` only after the complete
  supervised acceptance checklist in `docs/harmony-g2.md` passes, including a verified
  final Off result. Evidence for one controller model or capability does not imply
  support for another.
- The public privacy scanner (`scripts/check_public_tree.py`) must pass before
  proposing any change.

## Governance checks

- Keep `CONTRIBUTING.md`, `SECURITY.md`, and `CODE_OF_CONDUCT.md` in sync with
  behavior changes.
- Verify command behavior through tests and report verification commands in PRs.

## Clean release verification

Passing checks in a working tree is not sufficient for a release. Untracked files,
ignored files, local dependency state, or an ambient `PYTHONPATH` can hide omissions
and make a build succeed only on the contributor's machine. Before proposing a
release, run the following sequence from the repository root to export only committed
`HEAD`, rebuild it without `PYTHONPATH`, and inspect both generated artifacts without
extracting their members:

```bash
set -euo pipefail
RESCUE_EXPORT_DIR="$(mktemp -d)"
trap 'rm -rf "$RESCUE_EXPORT_DIR"' EXIT
git archive --format=tar HEAD | tar -xf - -C "$RESCUE_EXPORT_DIR"
cd "$RESCUE_EXPORT_DIR"
env -u PYTHONPATH uv sync --extra dev
env -u PYTHONPATH uv run pytest -q
env -u PYTHONPATH uv run ruff check .
env -u PYTHONPATH uv run ruff format --check .
env -u PYTHONPATH uv run mypy src tests scripts
env -u PYTHONPATH uv run python scripts/check_public_tree.py .
env -u PYTHONPATH uv build
          uv venv --seed --python .venv/bin/python .smoke-env
env -u PYTHONPATH uv pip install --python .smoke-env/bin/python --no-cache dist/*.whl
env -u PYTHONPATH .smoke-env/bin/smartenit-rescue profiles list
env -u PYTHONPATH .smoke-env/bin/python -c "import smartenit_rescue.profiles as profiles; assert any(profile.profile_id == 'smartenit.4040c' for profile in profiles.iter_builtin_profiles())"
rm -rf .smoke-env
env -u PYTHONPATH uv run python scripts/verify_release_tree.py \
  dist/*.whl dist/*.tar.gz
if env -u PYTHONPATH uv tree | grep -E \
  '(^|[[:space:]])(file:|git\+ssh:|-e[[:space:]]|--editable[[:space:]])'
then
  echo "local or private dependency detected" >&2
  exit 1
fi
```
