# An example site file

A site file for a farm with a FlexLM licence server, with placeholder names.
`site.toml` shows the `[hosts]` and `[tools]` tables, and
`hooks/flexlm_free.sh` is the probe that turns `lmutil lmstat` output into
the `free total` line a tool probe prints. `docs/reference/configuration.md` lists
every key.

The core knows no licence manager. A site hook does that work, and the
driver reads one integer from it.
