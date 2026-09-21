# AGENTS.md

Gemini video analysis on Google Cloud. One container image serves two surfaces:
Agent Runtime for IAM-authenticated callers, Cloud Run for public callers
holding a shared token.

## Commands

```bash
uv sync                       # install, including dev dependencies
uv run pytest                 # tests
uv run ruff check .           # lint
uv run ruff format .          # format
terraform -chdir=terraform/foundation validate
```

CI runs lint, format check, tests, `terraform validate`, `shellcheck scripts/*.sh`,
and a container build before anything deploys. Run lint and tests before you
call a change done.

## Layout

| Path | What lives there |
|---|---|
| `app/` | The FastAPI service and everything it calls |
| `tests/` | pytest, one module per `app/` module |
| `terraform/foundation/` | Project-wide resources: buckets, service accounts, IAM |
| `terraform/agent/` | The Agent Runtime deployment |
| `scripts/` | Operational shell scripts, checked by shellcheck |

## Conventions

- **Python 3.12**, line length 100, ruff with `E`, `F`, `I`. Formatting is not a
  matter of taste here: `ruff format --check` fails the build.
- **Type hints on anything that crosses a module boundary.** Internal helpers
  can go without when the types are obvious from two lines away.
- **Comments explain why, never what.** A comment restating the code is worse
  than no comment, because it rots separately from it.
- **Tests name the behaviour, not the function**: `test_rejects_a_clip_longer_than_the_cap`,
  not `test_validate`. A failing test should read as a sentence about the product.
- **Never commit a project id, bucket name or token.** They come from the
  environment or Secret Manager. `scripts/` reads them from `gcloud config`.

## Working on this repo

- **Terraform `name` changes are destructive.** Renaming a resource replaces it,
  which for Firestore or a bucket means losing data. Rename the Terraform
  address, not the resource.
- **The Dockerfile serves both surfaces.** Agent Runtime and Cloud Run run the
  same image; a change that suits one must not break the other.
- **Prefer failing loudly over degrading silently.** If a video cannot be read
  or a credential is missing, raise it — a half-review that looks complete is
  worse than an error.
- **Keep the public API's shape stable.** A mobile client is deployed against
  it; additive changes are fine, renamed or removed fields are not.

## Commit messages

Conventional Commits: `type(scope): subject`, subject in the imperative and
under 72 characters. Explain in the body what changed and why, not how — the
diff already says how.

```
feat(review): cap an uploaded clip at ten minutes

A thirty-minute clip costs more to analyse than the result is worth, and
the model's attention drops off well before the end. Ten minutes keeps a
review inside one request and bounds the bill.
```
