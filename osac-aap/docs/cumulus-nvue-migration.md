# OSAC-7869: Cumulus provider migration to nvidia.nvue

Status: proposal (not yet accepted)
Owner: Connectivity&Fabric
Issue: [OSAC-7869](https://redhat.atlassian.net/browse/OSAC-7869)

## Summary

Replace the direct `nv` commands in network-runner's Cumulus switch
provider with modules from the [`nvidia.nvue`][galaxy] Ansible collection,
while preserving the switch-management behavior that AgentlessNet
(Agentless VLAN Fabric Manager) depends on: bridge-domain VLAN
create/delete, trunk/access port configuration, VLAN discovery, and the
deletion-guard serialization.

[galaxy]: https://galaxy.ansible.com/ui/repo/published/nvidia/nvue/

## Current state

The provider is vendored from the [`ybettan/network-runner`][nr] Git
collection (version 0.4.0, see `collections/requirements.yml`) at
`vendor/ansible_collections/ansible_network/network_runner/roles/switch/providers/cumulus/`.

[nr]: https://github.com/ybettan/network-runner

Every mutating task (`create_vlan`, `delete_vlan`, `conf_access_port`,
`conf_trunk_port`, `add_trunk_vlan`, `delete_trunk_vlan`, `delete_port`)
runs one `raw` shell snippet over SSH that:

1. takes `flock /tmp/nvue.lock` to serialize with the NVUE config lock;
2. for guarded tasks, fails with exit code 73 when the guard file under
   `/var/lib/network-runner/guards` exists (delete-wins semantics);
3. runs `nv set`/`nv unset`;
4. validates `nv config diff --output commands` emits only
   `nv set`/`nv unset` lines;
5. runs `nv config apply -y` when there is a pending diff.

Read tasks (`list_vlans`, `get_port_conf`) use `bridge -j vlan show`,
`nv show system global reserved vlan --output json`, and `awk` over
`/etc/cumulus/switchd.conf`. `set_deletion_guard` uses only `flock` and
`install` (no `nv`).

## Target design

### Module choice: `nvidia.nvue.command`

`nvidia.nvue` offers two transports:

- object-specific and `nvidia.nvue.api` modules speak the NVUE REST API
  and require `ansible.netcommon.httpapi` plus the NVUE REST endpoint
  (port 8765) enabled on every switch;
- `nvidia.nvue.command` is a wrapper around the `nv` CLI with Jinja
  templating, `apply`, and `assume_yes`, running over the existing SSH
  connection.

This migration uses **`nvidia.nvue.command`**. It removes the direct
`nv` invocations (the issue's scope) without changing the management
transport, the switch prerequisites, or the inventory contract
(SSH-reachable Cumulus switches, no REST endpoint, no new credentials).
A later, separate proposal may move to the REST modules.

### Mutating tasks

Each mutating provider task becomes a guarded pair:

1. a `raw` pre-check that keeps the current semantics without invoking
   `nv`: take `flock /tmp/nvue.lock`, check the guard file, release the
   lock. Exit 73 stays the refusal signal so AgentlessNet's delete-wins
   handling is unchanged.
2. `nvidia.nvue.command` with a `template` rendering the same
   `nv set`/`nv unset` statements, `apply: true`, `assume_yes: true`.

The `flock` serialization cannot wrap a module call, so the provider
instead relies on NVUE's own apply-time locking (the same lock
`/tmp/nvue.lock` mirrors) plus the play-level pre-check. The
diff-validation step (`nv config diff`) is dropped: it guarded against
unexpected pending configuration from other actors, which the module's
apply already supersedes; this behavioral delta is called out below.

### Read tasks

`list_vlans` and `get_port_conf` are **not migrated in this pass**.
They read kernel bridge state (`bridge -j vlan show`) and reserved-range
metadata, not NVUE configuration; the NVUE equivalents would change the
discovery semantics that `tests/integration/targets/agentless_net_switch`
asserts (normalized VLAN discovery, reserved ranges). This gap is
documented per the issue's acceptance criteria and tracked for a
follow-up.

### Deletion guard task

`set_deletion_guard` and `delete_port`'s guard check stay `raw` shell
(no `nv` usage); unchanged.

## Delivery path (vendored content constraint)

`vendor/` content must not be hand-edited
([osac-aap/AGENTS.md](../AGENTS.md)). The provider change therefore
lands in two places, in order:

1. **Upstream PR to `ybettan/network-runner`**: the provider task
   changes above, its own molecule/unit tests, a `requirements.yml` or
   `meta/runtime.yml` dependency declaration on `nvidia.nvue`, and a
   version bump to 0.5.0.
2. **OSAC mono-repo PR** (this branch's scope once upstream releases):
   - bump `ansible_network.network_runner` to `0.5.0` in
     `collections/requirements.yml`;
   - add `nvidia.nvue` (Galaxy-published, GPL-2.0-or-later, requires
     ansible-core >= 2.15, which the EE already satisfies) to
     `collections/requirements.yml`;
   - run `make vendor-collections` (needs Automation Hub credentials)
     and review the complete `vendor/` diff, per the component rules;
   - rebuild the execution environment
     (`make execution-environment-build`) since the EE image must
     carry `nvidia.nvue` for AAP job runs;
   - update the fixtures and tests below;
   - update `docs/INTEGRATION-TESTING.md` and the touched-area map only
     if commands or coverage boundaries change.

## Test impact

| Area | Change |
|---|---|
| `tests/integration/fixtures/agentless_switch/ssh` | The fake endpoint currently parses the exact `nv set`/`nv unset`/`nv config apply` shell lines. With `nvidia.nvue.command`, Ansible ships a Python module to the switch over SSH instead of a shell snippet; the fixture must either emulate the module protocol or expose a minimal `nv`/`python3` surface. Prototype during the upstream PR, not before. |
| `tests/integration/targets/agentless_net_contract` | Partial switch-apply retry, delete-wins guard refusal (exit 73), port restoration: keep asserting, adjust to the new failure surfaces. |
| `tests/integration/targets/agentless_net_switch` | VLAN discovery and trunk reconciliation: unchanged (read tasks not migrated). |
| `tests/unit` (AgentlessNet switch-output mocks) | Update mocked command output where provider task names or command shapes change. |
| Wire goldens (`tests/contract/network-task`) | No change: the manager task contract, inputs, and results are untouched. |
| Coverage gate | Any new `when`/`rescue`/fail path in the provider needs a test or an entry in `tests/coverage/exclusions.yaml` — note the coverage gate measures `roles/netris`, `roles/cudn_evpn` only; provider files under `vendor/` are reported, not gated. |

## Behavioral deltas and risks

- **Diff validation removed** (see above): a pending unrelated NVUE
  change could be applied together with the provider's change. NVUE
  apply is atomic per revision, so the blast radius is one apply
  carrying both changes. Flag to reviewers; keep if unacceptable.
- **Guard/apply no longer atomic under one lock**: the guard check and
  the module's apply are separate steps. The window between them is the
  same one the current `flock` design closes; mitigation is that
  `nvidia.nvue.command`'s apply still takes the NVUE config lock
  internally. If the delete-wins race reappears in contract tests, wrap
  the module call in a `flock`-guarded `ansible.builtin.script`
  fallback instead.
- **Collection support level**: `nvidia.nvue` is community-supported
  ("best effort", validated on Cumulus Linux 5.4+). The provider's
  supported Cumulus floor should be stated as 5.4+ in the network-runner
  README.
- **Live-switch equivalence**: acceptance requires lab validation on a
  real Cumulus switch comparing `nv config show` before/after for the
  same task sequence. That is QE work under the
  [OSAC-4843](https://redhat.atlassian.net/browse/OSAC-4843) suite, not
  covered by the component targets.

## Acceptance criteria mapping

- Cumulus provider uses `nvidia.nvue` for the migrated operations:
  mutating tasks migrate; read tasks and guard plumbing are documented
  gaps with rationale.
- Collection dependencies declared: upstream `nvidia.nvue` requirement
  in network-runner plus `collections/requirements.yml` in OSAC.
- Equivalent behavior: contract targets assert guard refusal, retry and
  restoration semantics; live-switch equivalence is QE-run under
  OSAC-4843; remaining gaps documented above.
