# Optional Kubernetes/k3s edge integration (SF-305)

**Status: optional prototype.** This lets SuiFleet coexist with a cluster that a
customer already owns. Conventional Linux, Yocto and Pi pilots do not need it, and
it does not block their release. The daemon, packages and images take no new
Kubernetes dependency, and nothing is installed into a cluster automatically.
It has no physical-device, mainnet, production-cluster or provider acceptance.
See [Validation](#validation) for what actually ran.

## Integration modes

| Mode | Status | Summary |
|---|---|---|
| Coexistence | Default, no code | The SuiFleet client runs on the host as usual (systemd/Yocto service). The cluster and its workloads are not touched. SuiFleet still manages host components, the client and platform-script OTA. |
| Namespaced workload publisher | **Selected prototype** | A supervised SuiFleet component applies an authenticated workload bundle into **one** administrator-delegated namespace, using a namespaced ServiceAccount. |
| SuiFleet client as a privileged DaemonSet/node agent | Unsupported | It would need host PID/mount/network access, the device key inside the cluster, and a second updater for the client. That conflicts with the signer and key isolation ([KEY_ISOLATION.md](KEY_ISOLATION.md)) and with [daemon self-update](DAEMON_UPDATE.md). |
| Cluster-admin operator, CRDs or cluster-scoped objects | Unsupported | No cluster-admin shortcut is available. Cluster-wide policy belongs to the cluster administrator. |
| Managing Kubernetes, k3s, nodes, the OS or firmware | Unsupported | Platform scripts and the cluster's own tooling still own these. SuiFleet never reboots a node or drains or upgrades a cluster ([platform boundary](../AGENTS.md#platform-update-boundary--user-requirement)). |

## Ownership model: who owns what

| Owner | Owns | Does not own |
|---|---|---|
| Cluster administrator (customer) | The cluster, nodes, the dedicated delegated namespace and its Pod Security level, the admission policy, `ResourceQuota`/`LimitRange`, the publisher's ServiceAccount, Role and RoleBinding, token issue and rotation, registry pull credentials, and all networking (`Service`, `Ingress`, `NetworkPolicy`) that exposes the delegated workloads | The delegated workload objects SuiFleet creates. The administrator can still delete them, which is how it takes them back. |
| SuiFleet (one component on one publisher device) | Only `Deployment` and `ConfigMap` objects in the delegated namespace. Each carries `app.kubernetes.io/managed-by=suifleet` and `suifleet.io/component=NAME`. | Anything else, including Services, objects it did not create, other components' objects and other namespaces |
| Customer GitOps/Helm/other controllers | Everything they create | SuiFleet-labelled objects. Do not point a second controller at them. |

The authoritative desired state for delegated objects is the SuiFleet deployment.
It consists of the on-chain deployment record and recipe, plus the workload bundle
stored as the component's Walrus artifact. Both recipe and artifact are
authenticated by their on-chain SHA-256 digests, like any other component. The
cluster holds runtime state only.

How the helper keeps two controllers from fighting:

1. **No adoption, and every write is bound to the object that was checked.**
   For each object the helper reads it and checks its ownership labels. An
   existing object without SuiFleet's ownership labels, or owned by another
   SuiFleet component, stops the apply with exit code 3 and is not changed.
   The write that follows cannot reach a different instance:
   - An absent object is created with **create-only** semantics. If someone
     else created it after the read, the create fails, and the helper re-reads
     it and refuses to adopt it.
   - An existing owned object is updated by server-side apply that carries the
     observed `metadata.uid` and `metadata.resourceVersion` as preconditions.
     If the object was replaced, relabelled or otherwise changed after the
     check, the API server rejects the write. The helper then re-reads the
     object and re-checks ownership, a bounded number of times, and never
     writes blindly.
   - Right after its own create, the helper converts that create's field
     ownership into its server-side apply ownership with a merge patch carrying
     the same UID/resourceVersion preconditions. Otherwise later applies would
     conflict with SuiFleet's own creation.
2. **Server-side apply, one field manager, never forced.** Every apply uses
   field manager `suifleet` without `--force-conflicts`. Suppose another manager
   (`kubectl edit`, a GitOps tool, an autoscaler) has taken a field that the
   bundle sets. The apply then fails with a conflict (exit code 3) and leaves
   the other value in place. It reports the problem and does not overwrite the
   value. To recover, the other controller relinquishes the field, or the
   administrator deletes the object. The next apply then succeeds.
3. **Pruning is label-scoped and preconditioned.** After every object has been
   applied, the helper lists objects that carry this component's labels in the
   delegated namespace and are absent from the current bundle. It deletes each
   one with the listed UID and resourceVersion as delete preconditions. If an
   object was relabelled or replaced after the listing, the delete fails; the
   helper re-reads it and retries with a refreshed resourceVersion only while
   it is the same instance (the listed UID) and still this component's
   object. A replacement is left alone, even one that carries this
   component's labels. An object that is already terminating (for example,
   held by a finalizer) is not deleted again. Deletes use foreground
   cascading deletion: a pruned Deployment stays visible, terminating and
   still labelled, until the garbage collector has removed its ReplicaSets and
   their Pods. Readiness therefore sees old Pods that still run, without the
   publisher needing any Pod or ReplicaSet permission. Updates and rollbacks
   remove the other version's extra objects.
   Customer objects, other components and other namespaces are never deleted.
4. **Autoscalers.** If a `HorizontalPodAutoscaler` (created by the administrator)
   scales a SuiFleet Deployment, omit `spec.replicas` from the bundle. Otherwise
   the two managers would conflict, as in item 2.
5. **One publisher per component and namespace.** Do not target the same
   component and namespace from two SuiFleet devices. They would both claim
   ownership and could apply different versions. This is unsupported, and the
   labels cannot detect it.
6. **A multi-object apply is not transactional.** Objects are written one at a
   time, in bundle order. If a later write fails (a conflict, a rejection or a
   cluster error), earlier objects may already carry the new bundle, and
   nothing is pruned in that run. `ready` keeps failing until every object
   carries the new bundle's digest and pruning has converged, so the
   supervisor does not see a partial apply as ready. A later successful apply
   converges, and a supervisor rollback re-applies the previous bundle.

Stopping or restarting the SuiFleet client leaves the workload running. The
cluster keeps serving the last applied bundle. To retire the workload, deploy a
version whose bundle has an empty `resources` list, or have the operator run
`suifleet_k8s_apply.py remove --profile NAME --component NAME`. A retirement
bundle is ready only once no object labelled for the component remains.
`remove` deletes the component's objects with foreground deletion and then
waits, up to `--timeout` seconds (default 300), until no object labelled for
the component remains. A pruned Deployment stays listed until its ReplicaSets
and Pods are gone. `remove` exits 0 only after that convergence. It exits 1
(`not removed: …`) while an object remains, for example when a finalizer
holds a Pod or an object was replaced between listing and deletion. Running
it again does not re-delete terminating objects; it waits again and reports
the current state.

## Security model

- **Credential.** The administrator issues a ServiceAccount token for the
  publisher. Prefer a bounded `kubectl create token --duration`, and the
  administrator rotates it. The administrator writes a kubeconfig for that
  account into the device-local profile `/etc/suifleet/k8s/PROFILE/`. Recipes,
  bundles, Walrus artifacts and chain objects never contain cluster
  credentials. The helper rejects a profile in any of these cases:
  - The kubeconfig is group- or other-accessible (it must be `0600`).
  - The profile directory or files are writable by others, or are symlinks.
  - The kubeconfig does not select exactly one context, cluster and user.
  - It skips TLS verification or lacks a pinned certificate authority.
  - It uses `exec`/`auth-provider` plugins, impersonation or basic
    authentication.
  - It does not use exactly one credential method: a token (`token` or
    `tokenFile`, not both) or a complete client certificate and key pair
    (inline data or file, each set once). Mixed and incomplete credentials
    are rejected.

  `KUBECONFIG`, contexts and home-directory credentials are never inherited,
  and kubectl runs with a private temporary cache.
- **Authorization.** [`scripts/kubernetes/cluster-setup.yaml`](../scripts/kubernetes/cluster-setup.yaml)
  grants a namespaced Role with `get, list, create, patch, delete` on
  `configmaps` and `apps/deployments`. Nothing else is granted: no Services or
  other networking objects, Secrets, Pods, `exec`/`attach`/logs,
  ServiceAccounts or tokens, RBAC, quotas, namespaces, nodes, CRDs or
  cluster-scoped objects. Services stay administrator-owned, so the publisher
  credential cannot create Services, Ingresses or NetworkPolicies, or change
  their selectors or ports.
- **Delegated Pod labels select Service backends.** The publisher sets the
  Pod template labels of its Deployments. Any administrator-owned Service
  (or other selector-based object) whose selector matches those labels sends
  its traffic to the publisher's Pods, as the `app=web` Service in the local
  example does. Delegating a workload therefore also delegates the backends
  of every Service that selects its Pods. The publisher can also attach Pods
  to a Service intended for another workload in the namespace by copying its
  labels. Administrators must treat exposing such a Service as trusting the
  publisher with that traffic. Do not expose a Service from this namespace
  that the publisher should not be able to serve. This is another reason the
  namespace must be dedicated to the publisher.
- **Trust boundary: a dedicated namespace.** Whoever holds the publisher
  credential can create or change Deployments in the namespace directly,
  without the helper. The helper's checks therefore protect nothing against
  that holder. The cluster enforces the boundary:
  - The namespace must be **dedicated** to the publisher. Do not put unrelated
    Secrets, other workloads, privileged ServiceAccounts or RoleBindings for
    the namespace's `default` ServiceAccount in it. Anything a pod in the
    namespace can reach through its spec is within the publisher's reach.
  - Pod Security Admission `restricted` rejects privileged, root,
    host-namespace, hostPath and added-capability pods.
  - The `ValidatingAdmissionPolicy` in the setup file enforces the
    credential-level restrictions that PSA does not cover, for every
    Deployment and pod in the namespace. It requires the `default`
    ServiceAccount, `automountServiceAccountToken: false`, no
    `imagePullSecrets`, only `configMap`/`emptyDir` volumes, no Secret
    references in `env`/`envFrom`, and `@sha256`-pinned images. It needs
    Kubernetes 1.30 or later. On a cluster without it, the
    administrator must enforce the same rules with another admission
    controller. Otherwise, Secret and ServiceAccount isolation rests only on
    the namespace really being dedicated.
  - A `ResourceQuota` and `LimitRange` bound what can be scheduled and forbid
    NodePorts and LoadBalancers.
- **Helper validation.** This is early, explainable failure on top of the
  cluster's boundary, not a replacement for it. The helper accepts only
  Deployments and ConfigMaps, at most 64 objects in a bundle of at most
  1 MiB. Malformed field types are rejected (exit code 2) like any other
  invalid bundle. It rejects any of the following:
  - a namespace chosen by the bundle, or reserved ownership labels
  - host networking, PID or IPC, `nodeName`, host ports or `volumeDevices`
  - volumes other than `configMap`/`emptyDir`
  - Secret references (volumes, `env`, `envFrom`), projected tokens,
    `serviceAccountName` or `imagePullSecrets`
  - privileged mode, privilege escalation, root UID or added capabilities
  - images not pinned by `@sha256`

  It also forces `automountServiceAccountToken: false`. A pod can read a Secret
  through its spec without any RBAC on Secrets, so the helper never lets a
  bundle reference one. Applications that need secrets must use an
  administrator-provisioned mechanism outside the bundle.
- **Device trust.** Running the helper needs `apply.allow_recipe_commands = true`.
  Enable that only for trusted fleet operators, as for every lifecycle-command
  component ([OPERATIONS.md](OPERATIONS.md)). Operators install the helper and
  kubectl on the publisher host. Recipes do not download them.

## Hardware identity

The SuiFleet device identity belongs to the host that runs the SuiFleet client,
and its signing key stays in that host's signer
([KEY_ISOLATION.md](KEY_ISOLATION.md)). The key is never mounted into the
cluster or exposed to pods. Receipts attest that this device ran the
helper and report its result. They do not attest:

- the integrity of the pods
- any other node of a multi-node cluster
- that the cluster enforces its policies

Kubernetes nodes and ServiceAccounts are not SuiFleet devices. Hardware-backed
attestation of the pods is out of scope.

## Recipe and setup

1. With the **administrator's** credentials, apply
   `scripts/kubernetes/cluster-setup.yaml`, after adjusting the namespace
   (everywhere, including the policy binding's namespace selector) and quota.
   Create any `Service` that should expose the workloads yourself, for
   example a ClusterIP Service selecting `app=web` on port `http`.
2. Mint a token, for example
   `kubectl create token suifleet-publisher -n suifleet-apps --duration=24h`,
   and write a kubeconfig with the CA data, server and token. Install it as
   `/etc/suifleet/k8s/edge/kubeconfig`, mode `0600`, owned by the daemon user.
   Add `/etc/suifleet/k8s/edge/profile.json`:

   ```json
   {"version": 1, "namespace": "suifleet-apps", "kubeconfig": "kubeconfig", "kubectl": ["/usr/local/bin/kubectl"]}
   ```

   `kubectl` must be an absolute path, because lifecycle commands get a minimal
   `PATH`. On k3s, a symlink named `kubectl` to the k3s binary works. Plan
   rotation before the token expires; an expired token fails the apply and
   readiness.
3. Install `scripts/suifleet_k8s_apply.py` on the publisher host, for example as
   `/usr/local/libexec/suifleet/suifleet_k8s_apply.py`. It needs Python 3.9 or
   later and no third-party modules.
4. Publish a component whose artifact is the bundle JSON (see
   [`scripts/kubernetes/example-bundle.json`](../scripts/kubernetes/example-bundle.json)).
   Its recipe lifecycle looks like this:

   ```json
   "lifecycle": {
     "install": "/usr/bin/python3 /usr/local/libexec/suifleet/suifleet_k8s_apply.py validate --bundle {artifact}",
     "run": "exec /usr/bin/python3 /usr/local/libexec/suifleet/suifleet_k8s_apply.py run --profile edge --bundle {artifact} --interval 30",
     "readiness": "/usr/bin/python3 /usr/local/libexec/suifleet/suifleet_k8s_apply.py ready --profile edge --bundle {artifact}",
     "restart": "on-failure"
   }
   ```

`run` applies, prunes, then re-applies every interval, so an owned object that
someone deletes is recreated. It exits non-zero on a conflict, rejection or
cluster error, and the supervisor's restart budget then applies. `ready`
succeeds only when every object in the bundle exists, is not being deleted, is
owned by this component and carries *this* bundle's digest, version and
labels. ConfigMaps must also hold the bundle's data, and Deployments must be
observed, updated and available. A pre-existing foreign object with the right
name, an object of another component and an object from a stale bundle are
all not ready, also for ConfigMap-only bundles. `ready` also requires prune
convergence: while any other object labelled for this component remains in
the namespace, including one still terminating, it reports that object as not
yet pruned. This applies to updates, rollbacks and empty retirement bundles.
Because pruning is foreground, a pruned Deployment counts as remaining until
its ReplicaSets and Pods are gone, so a retirement is not ready while the old
workload can still serve through an administrator-owned Service.
A replacement that never becomes ready
fails, and the supervisor restores the previous version. That version's `run`
re-applies its bundle and prunes the failed version's extra objects.

Exit codes: `0` success, `1` not ready, `2` rejected (bundle, profile,
credential or cluster error), `3` ownership or field-manager conflict.

## Unsupported cases

- Running or updating the SuiFleet client in Kubernetes, or using the device key
  from a pod.
- Cluster-scoped objects, CRDs, multiple namespaces per profile, and kinds other
  than Deployment and ConfigMap. This includes Services, DaemonSets, Jobs,
  StatefulSets, PVCs, Ingress, NetworkPolicy, RBAC and Secrets. Networking is
  administrator-owned.
- Sharing the delegated namespace with unrelated Secrets, workloads or
  privileged ServiceAccounts, or running without the admission policy (or an
  equivalent administrator admission control).
- Helm charts, Kustomize or YAML bundles. Render them to the JSON bundle
  offline, then publish that.
- Adopting existing objects, or forcing ownership of fields held by another
  manager.
- Node, OS, Kubernetes/k3s, container-runtime or firmware installation, upgrade,
  drain or reboot.
- Exec-plugin or cloud IAM credentials, impersonation, and kubeconfigs
  without TLS verification.

## Validation

Offline unit tests run in normal CI. They cover bundle, profile and credential
validation (including every nested field replaced by every JSON type),
rendering, ownership, pruning and readiness decisions, and the
precondition-bound write protocol against a scripted fake kubectl:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts/tests -p test_suifleet_k8s_apply.py -v
```

The local-cluster acceptance needs Docker able to run one privileged container
and network access to pull the digest-pinned images. It starts a disposable,
digest-pinned k3s v1.31.4 server published only on `127.0.0.1` and removes it
afterwards. It runs every helper step with the narrow ServiceAccount
credential only:

```bash
python3 scripts/test_kubernetes_edge.py --supervisor --report /tmp/k8s-edge-report.json
```

It checks the following:

- RBAC allows and denies the expected operations, including Services and
  other networking objects and cluster-wide `*`.
- Pod Security Admission rejects a privileged pod applied directly with the
  publisher credential.
- The admission policy denies Deployments applied directly with the publisher
  credential that reference a Secret (`env`, `envFrom`, volume), use a
  non-default ServiceAccount, automount a token, use `imagePullSecrets` or an
  unpinned image.
- The helper refuses insecure and group-readable kubeconfigs, and rejects a bad
  bundle offline.
- Apply, rollout readiness and the content an administrator-owned Service
  actually serves work.
- Readiness of a ConfigMap-only bundle fails while the object is missing, is a
  pre-existing foreign object with matching data, belongs to another
  component or comes from a stale bundle digest, and succeeds once applied.
- Ownership labels are set, the field manager is single, and the creation's
  ownership was transferred to server-side apply.
- The helper does not adopt an existing object, and does not override a field
  owned by another manager. Recovery works after that manager relinquishes the
  field.
- A kubectl wrapper injects administrator actions between the helper's
  ownership check and its write. A competing creation, a replacement by a
  customer object and a relabel are each refused (exit code 3) and left
  unchanged. Pruning leaves a prune candidate alone when it is relabelled or
  replaced between the listing and the delete, including by a replacement
  that carries this component's labels. Readiness then reports the surviving
  object as not yet pruned.
- A finalizer-delayed prune keeps `ready` failing until the object is gone,
  and re-applying meanwhile does not re-delete it. An empty retirement bundle
  is not ready before its apply and is ready once pruning has converged.
- Dependent cleanup: while a finalizer holds a Pod of a pruned Deployment, the
  Deployment stays terminating (with the `foregroundDeletion` finalizer), its
  ReplicaSet and Pod remain, an empty retirement bundle is not ready and
  re-applying does not re-delete. It becomes ready only after the Pod is
  released and the Pod, ReplicaSet and Deployment are gone. `remove` with a
  held Pod exits 1 at its timeout and leaves the terminating Deployment.
  A retry does not re-delete and exits 1 again. After the Pod is released,
  `remove` exits 0, and at that point no object or Pod of the component
  remains.
- An update followed by a rollback prunes the extra object.
- Run mode re-creates a deleted object, stops on SIGTERM and leaves the
  workload running.
- With `--supervisor`, the real SuiFleet supervisor deploys a bundle, fails
  readiness on a broken image and rolls back with pruning. It then retires the
  component with an empty bundle while finalizers hold one deleted object and
  the pruned Deployment's Pod, and the deployment becomes ready only after the
  hold is released. No Pod or ReplicaSet of the pruned Deployment remains. This is
  an ignored Rust test,
  `kubernetes_publisher_real_cluster_rollout_rollback_and_retirement`.
- Removal deletes only this component's objects. A decoy object in another
  namespace is never touched.

Recorded run (2026-11-24, SF-305, after the third review repair):
`--supervisor` passed all 102 checks in 339 s. It ran on x86-64 WSL2 with
Docker, k3s v1.31.4+k3s1 and Rust 1.94.0. The offline unit tests passed 35 of
35. This run is a point-in-time record,
not a substitute for rerunning the tests after changes.

The test node uses absolute 512 MiB kubelet eviction thresholds, which relaxes
disk-pressure eviction so that a nearly full
development disk does not taint it. This changes only that disposable node.

This test uses no chain, wallet, Walrus, public network, production cluster or
credential, physical device or firmware updater. The supervisor test drives
the real component supervisor directly, not a chain-delivered deployment, and
the existing daemon/simulator suites still cover chain delivery.

Tests on a physical device or Pi, and on clusters other than the pinned local
k3s (managed Kubernetes, multi-node, other CNIs, admission controllers or
registry policies), remain unrun. So do production credential and
administrator processes. They are deferred to final acceptance in
[SF-031/#32](https://github.com/tyler-suifleet/suifleet/issues/32) and
[SF-087/#159](https://github.com/tyler-suifleet/suifleet/issues/159).
