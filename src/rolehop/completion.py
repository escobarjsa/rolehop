"""`rolehop completion`: shell completion scripts generated from the argument parser.

Subcommands and flags come from the parser itself, so they never drift. Organization, account and profile
names are completed dynamically through the hidden `rolehop _complete` command, which only reads orgs.toml.
"""

from __future__ import annotations

import argparse
import sys

from rolehop import config, errors

# Positional completions per subcommand: kind passed to `rolehop _complete`.
DYNAMIC = {
    "login": "orgs", "status": "orgs", "refresh": "orgs", "discover": "orgs",
    "remove": "targets", "envrc": "profiles", "agent": "agents", "completion": "shells",
}
HIDDEN = {"hook", "_mark", "_which-org", "_complete"}


def spec(parser: argparse.ArgumentParser) -> dict[str, tuple[str, list[str]]]:
    """{subcommand: (help, [flags])} for every visible subcommand."""
    out: dict[str, tuple[str, list[str]]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {a.dest: a.help or "" for a in action._choices_actions}
            for name, sub in action.choices.items():
                if name in HIDDEN:
                    continue
                flags = sorted(o for a in sub._actions for o in a.option_strings if o.startswith("--"))
                out[name] = (helps.get(name, ""), flags)
    return out


def names(kind: str) -> list[str]:
    """Names to complete for a kind; empty when orgs.toml is missing or invalid."""
    if kind == "shells":
        return ["bash", "zsh", "fish"]
    if kind == "agents":
        from rolehop import agents
        return ["list", "install", "uninstall", "rules", *agents.AGENTS]
    try:
        orgs = config.load_orgs()
    except errors.ConfigError:
        return []
    org_names = [o["name"] for o in orgs]
    profiles = [r["profile"] for r in config.catalog(orgs)]
    if kind == "orgs":
        return org_names
    if kind == "profiles":
        return profiles
    accounts = [f"{o['name']}-{a['name']}" for o in orgs for a in o["accounts"]]
    return [*org_names, *accounts, *profiles]


def bash_script(parser: argparse.ArgumentParser) -> str:
    sp = spec(parser)
    cases = []
    for name, (_, flags) in sp.items():
        dyn = DYNAMIC.get(name, "")
        cases.append(f'    {name}) opts="{" ".join(flags)}"; dyn="{dyn}" ;;')
    return f"""# rolehop completion for bash. Load it with: eval "$(rolehop completion bash)"
_rolehop() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" sub="${{COMP_WORDS[1]}}" opts="" dyn=""
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "{' '.join(sp)} --help --version --verbose" -- "$cur"))
    return
  fi
  case "$sub" in
{chr(10).join(cases)}
  esac
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "$opts --help" -- "$cur"))
  elif [ -n "$dyn" ]; then
    COMPREPLY=($(compgen -W "$(rolehop _complete "$dyn" 2>/dev/null)" -- "$cur"))
  fi
}}
_awslogin() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  COMPREPLY=($(compgen -W "--device --force --help $(rolehop _complete orgs 2>/dev/null)" -- "$cur"))
}}
complete -F _rolehop rolehop
complete -F _awslogin awslogin
"""


def zsh_script(parser: argparse.ArgumentParser) -> str:
    return ("# rolehop completion for zsh. Load it with: eval \"$(rolehop completion zsh)\"\n"
            "autoload -U +X compinit && compinit -C\n"
            "autoload -U +X bashcompinit && bashcompinit\n" + bash_script(parser).split("\n", 1)[1])


def fish_script(parser: argparse.ArgumentParser) -> str:
    sp = spec(parser)
    lines = ["# rolehop completion for fish. Save it with:",
             "#   rolehop completion fish > ~/.config/fish/completions/rolehop.fish",
             "complete -c rolehop -f",
             "complete -c rolehop -n __fish_use_subcommand -l verbose -d 'print debug details'"]
    for name, (help_text, flags) in sp.items():
        desc = help_text.replace("'", "\\'")
        lines.append(f"complete -c rolehop -n __fish_use_subcommand -a {name} -d '{desc}'")
        for flag in flags:
            lines.append(f"complete -c rolehop -n '__fish_seen_subcommand_from {name}' -l {flag[2:]}")
        if dyn := DYNAMIC.get(name):
            cond = f"__fish_seen_subcommand_from {name}"
            lines.append(f"complete -c rolehop -n '{cond}' -a '(rolehop _complete {dyn} 2>/dev/null)'")
    lines += ["complete -c awslogin -f -a '(rolehop _complete orgs 2>/dev/null)'",
              "complete -c awslogin -l device -d 'force the device code flow'",
              "complete -c awslogin -l force -d 'log in even if a session exists'"]
    return "\n".join(lines) + "\n"


def cmd_completion(args) -> int:
    from rolehop import cli
    parser = cli.build_parser()
    script = {"bash": bash_script, "zsh": zsh_script, "fish": fish_script}[args.shell](parser)
    sys.stdout.write(script)
    return 0


def cmd_complete(args) -> int:
    for name in names(args.kind):
        print(name)
    return 0


def register(sub) -> None:
    s = sub.add_parser("completion", help="print a shell completion script (bash, zsh, fish)")
    s.add_argument("shell", choices=["bash", "zsh", "fish"])
    s.set_defaults(func=cmd_completion)
    s = sub.add_parser("_complete")
    s.add_argument("kind", choices=["orgs", "profiles", "targets", "agents", "shells"])
    s.set_defaults(func=cmd_complete)
