# Shared Golden publisher

This directory is the trusted release implementation for all Golden labs. A
private source repository supplies only its tagged content, release routing,
canonical grader, rubric, tests, and student update asset. The publisher never
executes source-provided release, strip, bundle, grader, policy, or upgrade
code.

## Source contract

The source must be an active private, non-template repository named
`NYCU-SDNFV/Golden-<Name>`, with `master` as its default branch. The checkout
must be an unchanged exact remote tag and that tag must be an ancestor of
`origin/master`.

`.release.json` schema 1 fixes the owner to `NYCU-SDNFV`, configuration
repository to `NYCU-SDNFV/classroom50`, branch to `main`, Pages URL to
`https://nycu-sdnfv.github.io/classroom50`, and default channel to `newbie`.
Routes are data-driven:

```json
{
  "schema": 1,
  "name": "Toolchain",
  "title": "Lab 0: Toolchain",
  "description": "Validate the course toolchain.",
  "owner": "NYCU-SDNFV",
  "config_repository": "NYCU-SDNFV/classroom50",
  "config_branch": "main",
  "pages_url": "https://nycu-sdnfv.github.io/classroom50",
  "default_channel": "newbie",
  "student_updates": "manual",
  "channels": {
    "newbie": {
      "classroom": "winlab-newbies",
      "slug": "lab0-toolchain",
      "template": "Golden-newbie-Toolchain"
    },
    "115-1": {
      "classroom": "sdnfv-115-1",
      "slug": "lab0-toolchain",
      "template": "Golden-115-1-Toolchain"
    }
  }
}
```

Each `(classroom, slug)` and template must be unique. A template must be named
exactly `Golden-<channel>-<Name>`. Tags are `<channel>-vX.Y.Z`; `vX.Y.Z` is an
alias for `newbie`, and a bare configured channel is accepted only as the
initial `0.1.0` alias.

`student_updates` should be explicitly set to `manual` for new adoption.
Manual mode publishes the immutable snapshot, canonical bundle, and Classroom
configuration, but never modifies accepted student repositories. Students use
the existing `make check-update` and `make update` flow. `pull_request` is
recognized but currently rejected before mutation. The deployed organization
credential is intentionally limited to Contents and Administration publication
operations; the publisher does not call Pull requests or Actions APIs. Manual
mode is never a fallback after an API failure.

The canonical rubric at `.github/grade/tests.json` may contain any positive
number of uniquely named run tests; integer points must total exactly 100.
Protected student bytes are defined by
`.github/policy/manifest.sha256`. Executable modes come from the tagged Git
index, not the runner filesystem.

## CLI

```sh
RELEASE_TOKEN=... \
SOURCE_REPOSITORY=NYCU-SDNFV/Golden-Toolchain \
python3 tools/release/release.py \
  --source /path/to/private-source \
  --tag newbie-v0.1.0 \
  --report release-report.json
```

The credential needs Contents and Administration write access to the target
templates and `classroom50`, plus Metadata read access. No force push is used.
The initial organization credential has no Pull requests or Actions access,
which is sufficient for explicit `student_updates: manual` operation.
Reruns require identical metadata and bytes; moved tags, downgrades, aliases
colliding at one version, untagged template commits, wrong privacy/template
state, or Pages byte mismatches stop the release.
An existing assignment backed by another template is not silently taken over;
use a fresh slug or perform an explicit teacher migration. Workflow concurrency
is repository-scoped. Concurrent writes from different source repositories are
protected by the config repository's fast-forward push requirement; a rejected
publication is reported as failed and may be replayed with the same immutable
tag, rather than overwriting another Lab's change.

## Reusable workflow

A source workflow first runs its Lab-specific reusable PoC, then calls the
publisher with `needs: verify`. It must pin both `uses` and `toolkit_ref` to the
same full 40-character Golden-Base commit:

```yaml
jobs:
  verify:
    uses: ./.github/workflows/poc-lab.yml
    with:
      source_ref: refs/tags/${{ inputs.tag || github.ref_name }}

  publish:
    needs: verify
    uses: NYCU-SDNFV/Golden-Base/.github/workflows/shared-release.yml@0123456789abcdef0123456789abcdef01234567
    with:
      tag: ${{ inputs.tag || github.ref_name }}
      toolkit_ref: 0123456789abcdef0123456789abcdef01234567
      auth_mode: token
    secrets:
      release_token: ${{ secrets.GOLDEN_RELEASE_TOKEN }}
```

Do not use `secrets: inherit`. The caller `GITHUB_TOKEN` remains
`contents: read`; the publication credential exists only in the trusted
publish step. App mode instead supplies `app_id` and `app_private_key`; the
official token action revokes its installation token after the job. Reports
are uploaded as private caller-repository Actions artifacts even on failure.
