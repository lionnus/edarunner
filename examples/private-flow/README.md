# A template for a flow under NDA

This directory is a template, not a runnable flow: a synthesis stage and
a timing stage of commercial tools behind a public set of knobs. Copy it,
fill in every `<...>` of `nda/private.toml`, and point the paths at your
flow. [docs/guides/private.md](../../docs/guides/private.md) explains each
part.

```
site.toml                 the head node and the tools fc and pt, with placeholder names
project/                  the project directory, which an agent reads and edits
  edr.toml                two private stages, six knobs, metrics over the public files
  jobs/sweep.toml         a sweep that sets knobs and nothing else
  AGENTS.md, CLAUDE.md    the rules of the agent: what it never reads, how it asks for a knob
  .claude/settings.json   the sandbox that keeps every shell command but edr away from the private paths
nda/                      stands for the private repository a person keeps, outside the project
  private.toml            the commands, the line of each knob, the redaction and the publish
```

In a lab `nda/` is a clone of a private repository, or the private part of
the flow repository, in a directory that the agent's settings deny. Keep
the paths in `.claude/settings.json` in step with where it lies.

`tests/test_private.py::test_the_private_flow_template_loads` loads this
template, so it stays a valid project.
