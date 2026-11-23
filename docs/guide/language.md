# Language and output

## Choose the language

rolehop speaks **English**, **Spanish** and **Portuguese**. It follows your system language automatically; to choose one explicitly:

```bash
rolehop language es      # Español
rolehop language pt      # Português
rolehop language en      # English
rolehop language auto    # follow the system again
rolehop language         # show the current language and where it comes from
```

The choice is saved in `~/.config/rolehop/settings.toml` and applies to every command from then on. No reinstall is needed.

For a single run, or in a script:

```bash
rolehop --lang pt status
ROLEHOP_LANG=es rolehop doctor
```

### Which language wins

The first match wins:

| Order | Source | Example |
|---|---|---|
| 1 | `--lang` option | `rolehop --lang es status` |
| 2 | `ROLEHOP_LANG` environment variable | `export ROLEHOP_LANG=pt` |
| 3 | `rolehop language <code>` (saved setting) | `rolehop language es` |
| 4 | System locale: `LC_ALL`, `LC_MESSAGES`, `LANG` | `LANG=es_CO.UTF-8` |
| 5 | English | |

### What is translated, and what is not

Translated: every message, prompt, error, `--help` text and `doctor` explanation.

Kept in English on purpose:

- **`--json` output**, so scripts never break when someone changes the language.
- **Commands, flags, file names and profile names**, so you can copy and paste them.
- **Rules and hook messages for AI agents.** Agents work best with English instructions, and the rules tell them to answer you in your own language.
- **`--verbose` debug lines**, meant for bug reports.

## How output is designed

rolehop follows the [Command Line Interface Guidelines](https://clig.dev/):

- **Every problem comes with its fix.** A line that reports something wrong is followed by the exact command to run:

    ```console
    $ rolehop status
    ✓ acme      active · ~6h37m left (estimated) · account 111111111111
    ✗ personal  profiles are not in ~/.aws/config yet
                → run: rolehop sync

    1 of 2 organizations ready.
    ```

- **Symbols with one meaning each:** `✓` done, `!` needs your attention, `✗` failed, `→` the next command. No emoji.
- **Color only in a terminal.** Green, yellow and red are used only when output goes to a terminal. They are disabled with `--no-color`, `NO_COLOR=1` or `TERM=dumb`; `FORCE_COLOR=1` forces them.
- **Short by default.** Technical details (which profile was probed, commands run, files written) appear with `-v`.
- **Stable machine output.** Use `--json` in scripts; human-readable text may improve between versions.
