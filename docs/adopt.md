# Adopting a template into an existing project

The `copier adopt` command applies a template to an **existing** project that was
not originally created with Copier. Unlike `copier copy`, which bootstraps a new
project from scratch, `adopt` never destroys content that already exists at the
destination.

## When to use `adopt`

Use `adopt` when you have a pre-existing project (e.g. a legacy codebase) and
you want to apply a Copier template to it. Copier will:

- **Create** new files from the template.
- **Skip** files that already exist and match the template's rendered content.
- **Conflict** files that already exist but differ: the template version is
  written alongside the original as `<file>.copier-new`. The original is
  never overwritten.

## Basic usage

```bash
copier adopt [OPTIONS] template src
```

For example:

```bash
copier adopt --defaults https://github.com/username/template ~/my-project
```

By default `adopt` uses default answers to all questions (equivalent to
`--defaults`). Use `--force` as an alias for `--defaults`.

## Planning with `--plan` / `--pretend`

Before applying the template, you can preview what will happen:

```bash
copier adopt --plan https://github.com/username/template ~/my-project
```

This prints a table of planned actions (`create`, `conflict`, `skip`,
`skip-if-exists`) without writing any files. `--pretend` behaves identically
to `--plan`.

## Crash journal

Every `adopt` operation writes a crash journal at
`.copier-adopt.json` in the destination. The journal records each action taken,
as well as backups of any conflicting files stored under
`.copier-adopt-backup/`. This enables two operations:

### `--recover`

If an adopt operation is interrupted (e.g. a crash or `Ctrl+C`), you can
resume from the journal:

```bash
copier adopt --recover https://github.com/username/template ~/my-project
```

Files already applied are skipped; only remaining actions are processed.

### `--rollback`

To undo an adopt operation and restore the pre-adopt state:

```bash
copier adopt --rollback https://github.com/username/template ~/my-project
```

Rollback:
- Removes created files.
- Restores conflicting files from backups.
- Removes `.copier-new` conflict files.
- Deletes the journal and backup directory.

## Idempotency

After a successful adopt, the destination contains a `.copier-answers.yml` file
and is therefore managed by Copier. Re-running `copier adopt` without `--recover`
or `--rollback` will raise an error — use `copier update` for subsequent
template updates, or `--recover` to resume an interrupted adopt.

## Python API

```python
import copier

copier.run_adopt(
    "https://github.com/username/template",
    "~/my-project",
    defaults=True,
)
```

The `run_adopt` function accepts the same parameters as
[`run_copy`][copier.run_copy] (e.g. `answers_file`, `vcs_ref`, `exclude`,
`data`, `settings`, `user_defaults`) plus `plan`, `recover`, and `rollback`.
It returns the `Worker` used for rendering, consistent with `run_copy`.
