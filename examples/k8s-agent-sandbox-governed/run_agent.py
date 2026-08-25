#!/usr/bin/env python3
"""Upload and run a local script inside a kubernetes-sigs/agent-sandbox pod,
with every command policy-checked by AGT before it is dispatched.

kubernetes-sigs/agent-sandbox provides Kubernetes-native execution isolation
(pod-level, NetworkPolicy-scoped) but no semantic policy over *what* an
agent asks a sandbox to run — its own docs note network policy only covers
L3/L4, not command content. AGT's govern() fills that gap: it is evaluated
in this driving script's own process, before the command is sent to the
sandbox pod's execution API, so a denied command never reaches the pod at
all (rather than reaching it and being caught/mitigated by isolation).

Usage:
  pip install -r requirements.txt
  python run_agent.py my_tool.sh --interpreter bash -- --flag value
  python run_agent.py agent.py --warmpool python-warmpool --namespace agent-sandbox-demo
"""
import argparse
import re
import sys
from pathlib import Path

from agentmesh.governance import GovernanceDenied, govern
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

POLICY_PATH = Path(__file__).resolve().parent / "policy.yaml"

_DESTRUCTIVE_PATTERNS = [
    r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r",  # rm -rf / -fr, any flag order
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:",  # fork bomb
    r"curl[^|]*\|\s*(sh|bash)\b",
    r"wget[^|]*\|\s*(sh|bash)\b",
]
_CREDENTIAL_EXFIL_PATTERNS = [
    r"(curl|wget|nc)\b.*\$(AWS_[A-Z_]+|KUBECONFIG)",
    r"cat\s+.*kube/config.*(curl|nc)",
]


def _classify_command(command: str, script_content: str = "") -> str:
    """Pre-classify a command into a discrete action type for policy evaluation.

    AGT's policy condition DSL only supports equality/membership checks on
    context fields, not substring matching — so free-text pattern matching
    happens here, before the governed call. ``command`` is just the
    interpreter invocation (e.g. "bash foo.sh"); the actual risk usually
    lives in the uploaded script body, so ``script_content`` is scanned too.
    """
    combined = f"{command}\n{script_content}"
    if any(re.search(p, combined) for p in _DESTRUCTIVE_PATTERNS):
        return "destructive"
    if any(re.search(p, combined) for p in _CREDENTIAL_EXFIL_PATTERNS):
        return "credential_exfil"
    return "shell_exec"


def _run_in_sandbox(action: dict, *, sandbox, timeout: int):
    return sandbox.commands.run(action["command"], timeout=timeout)


def main() -> int:
    # argparse.REMAINDER greedily swallows everything after the script
    # positional, including our own flags (e.g. --warmpool foo) if they're
    # placed after it. Split on a literal "--" ourselves so our options can
    # appear anywhere before it, and only the script's own args follow it.
    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        own_argv, script_args = argv[:sep], argv[sep + 1:]
    else:
        own_argv, script_args = argv, []

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script", type=Path, help="Local path to the script to run in the sandbox")
    parser.add_argument("--interpreter", default="python3", help="Interpreter used to invoke the script (python3, bash, node, ...)")
    parser.add_argument("--warmpool", default="python-warmpool", help="SandboxWarmPool to draw the sandbox from")
    parser.add_argument("--namespace", default="agent-sandbox-demo", help="Namespace containing the warm pool")
    parser.add_argument("--timeout", type=int, default=60, help="Command execution timeout in seconds")
    args = parser.parse_args(own_argv)

    if not args.script.is_file():
        parser.error(f"script not found: {args.script}")

    remote_name = args.script.name
    command = " ".join([args.interpreter, remote_name, *script_args])

    # Read the script exactly once. Classifying and uploading from independently
    # taken reads would let a file swapped between the two reads be approved as
    # the safe version but executed as whatever the second read picked up.
    script_bytes = args.script.read_bytes()
    script_text = script_bytes.decode(errors="ignore")
    action_type = _classify_command(command, script_text)

    # Claim a sandbox pod only once the action is dispatched, so a denied
    # command never costs a warm-pool claim.
    sandbox = None

    def _dispatch(action: dict):
        nonlocal sandbox
        client = SandboxClient(connection_config=SandboxLocalTunnelConnectionConfig())
        sandbox = client.create_sandbox(warmpool=args.warmpool, namespace=args.namespace)
        sandbox.files.write(remote_name, script_bytes)
        return _run_in_sandbox(action, sandbox=sandbox, timeout=args.timeout)

    governed_run = govern(
        _dispatch,
        policy=str(POLICY_PATH),
        agent_id=f"run_agent:{args.namespace}",
    )
    try:
        try:
            result = governed_run(action={"type": action_type, "command": command})
        except GovernanceDenied as e:
            print(f"Command blocked by governance policy: {e}", file=sys.stderr)
            return 1

        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.exit_code
    finally:
        if sandbox is not None:
            sandbox.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
