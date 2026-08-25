# kubernetes-sigs/agent-sandbox + Agent Governance Toolkit

Govern the commands an agent sends to a [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
pod, before they are ever dispatched.

## Prior art

This example integrates with, and is not affiliated with, [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox),
a CNCF SIG project providing Kubernetes-native execution isolation (pod
sandboxing, per-template `NetworkPolicy`) for agent workloads. All CRDs,
the controller, and the sandbox-router referenced here come from that
project's own releases — this example only adds the client-side
governance wrapper around its Python SDK.

## Architecture

```
Agent driving script (run_agent.py)
     |
     v
_classify_command()   -- pre-classifies the command + uploaded script body
     |                    into a discrete action.type (policy DSL only
     |                    supports equality/membership, not substring match)
     v
govern(...)            -- evaluates policy.yaml against {"type": ..., "command": ...}
     |-- allow  --> sandbox.commands.run(...)  -->  agent-sandbox pod
     '-- deny   --> GovernanceDenied raised, pod never contacted
```

A denied command never reaches the sandbox pod at all — this is
defense-in-depth *before* isolation, not a substitute for it.

## Quick Start

### 1. Set up an agent-sandbox cluster

Follow the [agent-sandbox quickstart](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/quickstart/README.md)
to get a `python-sandbox-warmpool` running in the `agent-sandbox-demo`
namespace on any cluster (kind by default).

### 2. Install dependencies

```bash
cd examples/k8s-agent-sandbox-governed
pip install -r requirements.txt
```

`requirements.txt` has exactly two dependencies: `agent-governance-toolkit[full]`
(this repo — provides `govern()`/`GovernanceDenied`) and `k8s-agent-sandbox`
(the upstream Python SDK for talking to agent-sandbox's sandbox-router). No
other third-party packages are needed.

### 3. Run a benign script — allowed

```bash
python run_agent.py hello_world.py --warmpool python-sandbox-warmpool --namespace agent-sandbox-demo
```

```
Hello from a governed sandbox pod
```

### 4. Run a destructive script — denied before dispatch

```bash
python run_agent.py destructive.sh --interpreter bash \
  --warmpool python-sandbox-warmpool --namespace agent-sandbox-demo
```

```
Command blocked by governance policy: Action denied by policy rule 'block-destructive-commands':
Commands classified as destructive (rm -rf, mkfs, dd, disk wipes, fork bombs, pipe-to-shell installers) are blocked before dispatch.
```

Exit code is `1`, and `rm -rf /` never runs inside the pod — compare this to
relying on the pod's own isolation to contain it after the fact.

## Integration pattern

```python
from agentmesh.governance import govern, GovernanceDenied

governed_run = govern(
    lambda action: sandbox.commands.run(action["command"], timeout=60),
    policy="policy.yaml",
    agent_id="run_agent:agent-sandbox-demo",
)

try:
    result = governed_run(action={"type": action_type, "command": command})
except GovernanceDenied as e:
    print(f"blocked: {e}")
```

`policy.yaml` owns the allow/deny rules and audit trail; `run_agent.py` owns
pre-classifying free-text commands/scripts into the discrete `action.type`
values the policy conditions match against.

## Cleanup

`run_agent.py` already terminates the individual sandbox it creates
(`sandbox.terminate()` in a `finally` block) after each run, so nothing is
left behind per-invocation. To tear down the shared resources this example
relies on:

```bash
kubectl delete sandboxwarmpool python-sandbox-warmpool -n agent-sandbox-demo
kubectl delete sandboxtemplate python-sandbox-template -n agent-sandbox-demo
```

If you created a dedicated cluster just for this example, follow
[Step 10: Cleanup](https://github.com/kubernetes-sigs/agent-sandbox/blob/main/examples/quickstart/README.md#step-10-cleanup)
in the agent-sandbox quickstart (e.g. `kind delete cluster ...`) to remove
it entirely.

## Notes

- Tested against a local [EKS Anywhere](https://anywhere.eks.amazonaws.com/) (Docker provider) cluster; the same pattern works unmodified against `kind`, `minikube`, or any real cluster running agent-sandbox.
- `policy.yaml`'s two rules (`block-destructive-commands`, `block-credential-exfil`) are illustrative — extend `_classify_command()` and the policy's `rules` list for your own risk model.
