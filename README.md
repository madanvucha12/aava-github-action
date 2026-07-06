# AAVA Workflow — GitHub Action

Run an [AAVA](https://int-ai.aava.ai) workflow from CI: fire it with inputs, poll to completion, report
the result, and **fail the step if the workflow fails** — so you can use it as a required PR check.

It's a thin **host binding** over the AAVA core: the Action just runs the vendored `aava-exec` CLI
(under `core/`). Capability lives in core; GitHub is one more exposure.

## Usage

```yaml
- name: Run AAVA workflow
  id: aava
  uses: ascendionava/aava-github-action@v1
  with:
    workflow-id: "15654"
    inputs: |
      { "brd": "${{ github.event.pull_request.title }}" }
    token: ${{ secrets.AAVA_TOKEN }}
    comment-on-pr: "true"          # optional, pull_request events only

- run: echo "AAVA run ${{ steps.aava.outputs.run-id }} -> ${{ steps.aava.outputs.status }}"
```

### Inputs

| input | required | default | |
|---|---|---|---|
| `workflow-id` | ✓ | — | AAVA workflow (pipeline) id |
| `inputs` | ✓ | — | Workflow inputs as a JSON object string |
| `token` | ✓ | — | AAVA JWT — **pass via a repo secret** |
| `base-url` | | `https://int-ai.aava.ai` | AAVA API base |
| `timeout` | | `900` | Max seconds to wait |
| `comment-on-pr` | | `false` | Post the result as a PR comment (needs `pull_request` event + `GITHUB_TOKEN`) |

### Outputs

| output | |
|---|---|
| `run-id` | AAVA run id |
| `status` | `COMPLETED` \| `FAILED` \| `TIMEOUT` |
| `result` | full `aava-exec --json` payload |

### Gating a PR

The step exits non-zero when the workflow fails or times out, so adding it to a `pull_request` job and
marking that job a **required status check** gates merges on the AAVA run — the CI equivalent of the
Golden Pattern's HITL/AQG gate.

## Requirements

- A GitHub-hosted (or self-hosted) runner with **`python3` 3.9+** (the bundled core is stdlib-only — no
  `pip install`, no Docker).
- An **`AAVA_TOKEN`** repository secret.

## Maintaining

`core/` is a **committed, pinned** copy of the AAVA core (the Action runs the repo's files at the used
ref). Re-vendor from an `aava-plugin-core` checkout (pin to a tag for releases):

```bash
AAVA_CORE_SRC=/path/to/aava-plugin-core node scripts/sync-core.mjs
git add core && git commit -m "chore: re-vendor core @ <tag>"
```

See `examples/aava-workflow.yml` for a complete caller workflow.
