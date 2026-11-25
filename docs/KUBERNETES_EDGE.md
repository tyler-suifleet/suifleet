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
   time: all ConfigMaps first, then Deployments, each group in bundle order.
   Writing ConfigMaps first means a Deployment never references a
   content-addressed ConfigMap (item 7) that does not exist yet. If a later
   write fails (a conflict, a rejection or a cluster error), earlier objects
   may already carry the new bundle. A failed apply deletes nothing; pruning
   runs only after every write has succeeded, and a prune that fails part way
   (or one whose foreground deletions have not finished) leaves the namespace
   holding two bundles. Until one of those two converges,
   `apply` refuses any other bundle before writing (see Quota headroom in
   item 7).
   `ready` keeps failing until every object carries the new bundle's digest
   and pruning has converged, so the supervisor does not see a partial apply
   as ready. A later successful apply converges, and a supervisor rollback
   re-applies the previous bundle. A rollback can restore every object:
   stable-name ConfigMaps are mutable (bundles cannot make them immutable; see
   Helper validation), and a content-addressed ConfigMap is only ever
   recreated with the content its name denotes.
7. **Pods read the configuration their template names.** Kubernetes never
   restarts Pods when a ConfigMap they read changes. Values read through
   `env`, `envFrom` and `subPath` mounts stay as they were when the container
   started, and a whole-volume mount refreshes only after a kubelet sync
   delay. The kubelet also resolves ConfigMaps by namespace and name from a
   watch-based cache, so a Pod starting on a node can read data the API
   server has already replaced. With a mutable shared name, a
   configuration-only change, a rollback or a partially applied update could
   therefore leave Pods running other configuration behind a Deployment that
   looks healthy and current. The helper prevents this by content
   addressing. Every bundle ConfigMap that a bundle Deployment consumes
   (`envFrom` `configMapRef`, `env` `valueFrom.configMapKeyRef` or a
   `configMap` volume, in any container or init container) is published as
   `NAME-HASH`, where `HASH` is the first 16 hex digits of the SHA-256 of its
   data (canonical JSON), with `immutable: true` and the annotation
   `suifleet.io/config-name: NAME`. The helper rewrites the Deployment's
   references to that name. The API server refuses any data change to an
   immutable ConfigMap, so each published name denotes exactly one content,
   and whatever the kubelet has cached under that name is that content.
   - A configuration change produces a new name, so the Pod template changes
     and Kubernetes rolls the Pods out. A rollback restores the earlier name
     and the identical earlier template.
   - A partially applied update creates the new ConfigMap but leaves the
     previous one, which the live template still names, until a later apply
     succeeds. An old-template Pod that starts in between still reads the old
     data.
   - `ready` compares the live Pod template, and so the ConfigMap names, with
     the rendered one and waits for that rollout. It never reports a
     configuration change or rollback ready while a Pod may still read other
     configuration.
   - Re-applying a bundle, or a new bundle that keeps the ConfigMap data,
     keeps the name and restarts nothing; neither does a change to a ConfigMap
     that no Deployment consumes.
   - A successful apply prunes the content-addressed ConfigMaps of earlier
     bundles. Pods of the previous template keep the environment and files
     they already read. One that restarts before the rollout replaces it
     cannot start (`CreateContainerConfigError`) rather than starting with
     other data. Deployment ReplicaSet history therefore does not support
     `kubectl rollout undo`; roll back by deploying the previous bundle.
   - A live owned content-addressed ConfigMap whose content differs from what
     its name denotes (for example one recreated out of band) stops `apply`
     and `run` with exit code 2 before any write. If a ConfigMap no longer
     holds exactly the bundle's content right after its write, the apply
     stops with a conflict before any Deployment is written. Do not recreate
     a content-addressed ConfigMap out of band: the kubelet stops watching an
     immutable ConfigMap once it has cached it, so a Pod started while such
     an object existed can keep its content, which `ready` cannot observe.
     In the dedicated namespace only the publisher and administrators can
     write ConfigMaps.
   - ConfigMaps that no bundle Deployment consumes keep their bundle name and
     stay mutable, for consumers outside the bundle. Their names may not end
     in `-` and 16 lowercase hex digits, the shape reserved for
     content-addressed names, so no bundle can leave a mutable object at a
     name another bundle derives. An owned mutable ConfigMap found at a
     content-addressed name (a node may have cached it with other data)
     stops `apply` with exit code 2 before any write; an administrator must
     delete it.
   - Quota headroom. A changed consumed ConfigMap is created under its new
     name before the old one is pruned, so an update or rollback briefly
     holds two bundles' ConfigMaps. A bundle may contain at most 9
     ConfigMaps. The shipped quota of 20 ConfigMaps covers two such bundles
     plus the namespace's `kube-root-ca.crt` and one more ConfigMap. Raise
     the quota by twice the per-bundle limit for every additional SuiFleet
     component in the namespace. `apply` deletes nothing before every write
     has succeeded, not even a ConfigMap that looks like an unreferenced
     leftover of a failed apply: a partial apply may already have moved a
     Deployment's template off the last fully applied bundle's ConfigMaps,
     and those are exactly what a rollback re-applies. Stable ConfigMaps of
     the last fully applied bundle likewise stay with their data, for
     consumers outside the bundle. Every object of the last fully applied
     bundle therefore survives any number of failed applies, so re-applying
     it (the supervisor rollback) creates no object and always fits, and
     retrying the partially applied bundle itself fits within the two-bundle
     headroom. The prune after the writes is not atomic either: it can
     delete some of the previous bundle's objects and then fail, and a
     rollback must then recreate them. So while the component's live
     objects carry two bundle digests (a partial apply or a partial prune),
     `apply` refuses, with exit code 2 before any write, every bundle except
     those two. The supervisor rollback (the previous bundle) and a retry
     (the new one) are allowed; both stay within the two bundles'
     ConfigMaps, so the rollback always fits. An empty retirement bundle
     creates nothing and is also allowed. Once one of them succeeds, its
     prune removes the other's objects and any bundle can be applied again.
     If neither can succeed, `remove` the component or have an administrator
     delete the leftovers. Objects still terminating count too: when an
     update replaces a Deployment name, the old Deployment stays listed
     (foreground deletion) after the previous bundle's ConfigMaps are gone,
     and the rollback would have to recreate them, so another bundle is
     refused until that deletion completes. Only then, with no object of
     the previous bundle left, has the update's prune converged.

Stopping or restarting the SuiFleet client leaves the workload running. The
cluster keeps serving the last applied bundle. There are two ways to retire
the workload:

- **Managed (preferred).** Deploy a version whose bundle has an empty
  `resources` list. Its `run` reconciler prunes the old objects, and it is
  ready only once no object labelled for the component remains. The
  reconciler for that version keeps nothing alive, so no other controller
  works against the retirement.
- **Operator removal.** First stop the component's reconciliation: remove the
  component from the device's deployment, or stop the SuiFleet client on that
  device, so no `run` process for the component remains. The `run` recipe
  below re-applies its bundle every interval and recreates deleted objects.
  `remove` only acts on the cluster and cannot stop a reconciler. Then run
  `suifleet_k8s_apply.py remove --profile NAME --component NAME`.

`remove` deletes the component's objects with foreground deletion and then
polls until no object labelled for the component remains. A pruned Deployment
stays listed until its ReplicaSets and Pods are gone. `--timeout` (default
300 seconds) bounds this wait after the deletes are issued. The polling
kubectl calls run against that deadline: their API request timeout and process
limit shrink to the remaining time, so a slow API server cannot extend the
wait. The delete phase before it is not counted in `--timeout`. Each of its
kubectl calls has a fixed 30-second API request timeout and 60-second process
limit, with at most 8 attempts per object when it races a concurrent change.
With `--timeout 0`, `remove` only issues the deletes and reports
`not removed`. A negative, infinite or NaN `--timeout` is rejected (exit 2)
before the profile is loaded or any cluster call is made.

`remove` exits 0 when one poll observed that no labelled object remains. This
result describes that moment only. If a reconciler is still running, or
someone applies the component again, the objects can return after `remove`
exits 0. That is why stopping reconciliation is a prerequisite. `remove` exits
1 (`not removed: …`) when the deadline passes while an object remains, for
example when a finalizer holds a Pod or an object was replaced between listing
and deletion. Running it again does not re-delete terminating objects; it
waits again and reports the current state.

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
  - a Deployment that consumes a ConfigMap (`envFrom`, `configMapKeyRef` or a
    volume, including `optional` references) that the same bundle does not
    define. Its content could change without any rollout, so `ready` could not
    vouch for the configuration the Pods run. Configuration therefore travels
    in the bundle, and administrator-provided configuration needs a mechanism
    outside the bundle.
  - `immutable: true` on a ConfigMap. Kubernetes never lets an immutable
    ConfigMap's data change or the flag return to `false`, so a supervisor
    rollback could not restore a stable-name ConfigMap in place. The helper
    itself makes consumed ConfigMaps immutable under content-addressed names
    (see the ownership model, item 7), where no in-place change is needed.
    If an owned stable-name ConfigMap in the cluster has been made immutable
    out of band, `apply` and `run` refuse with exit code 2 before writing any
    object, so nothing is half-applied. The helper never deletes and
    recreates it automatically. An administrator must delete it; the next
    apply then recreates it as a mutable object.
  - a ConfigMap that no Deployment consumes whose name ends in `-` and 16
    lowercase hex digits, the suffix reserved for content-addressed names
    (item 7). This also rules out a collision between a content-addressed
    name and another ConfigMap of the same bundle.
  - more than 9 ConfigMaps (the quota headroom in item 7).

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
someone deletes is recreated. `--interval` (default 30 seconds) must be finite
and at least 1 second; a smaller, zero, negative, infinite or NaN interval is rejected
(exit 2) before the profile is loaded or any cluster call is made. It exits non-zero on a conflict, rejection or
cluster error, and the supervisor's restart budget then applies. `ready`
succeeds only when every object in the bundle exists, is not being deleted, is
owned by this component and carries *this* bundle's digest, version and
labels. ConfigMaps must also hold the bundle's data and no `binaryData` (the
bundle format has none, and a ConfigMap volume would project it as extra
files). Deployments must hold
every spec field the bundle specifies and be observed, updated and available,
so a healthy rollout of another manager's edit (for example a changed image or
pod template that keeps SuiFleet's labels and annotations) is not ready. A
live spec field the bundle omits must hold the API server's documented default
for it (for example `restartPolicy: Always`, probe thresholds, port `protocol`,
the RollingUpdate strategy, an HTTP probe or lifecycle `path` of `/` and
`scheme` of `HTTP`) or a zero value. An explicitly empty string for a
plain-string field the server defaults (such as an httpGet `path`) matches
that default too, as does an explicit `0` for a probe's `timeoutSeconds`,
`periodSeconds`, `successThreshold` or `failureThreshold` (non-pointer fields
the server defaults to 1, 10, 1 and 3). Pointer fields such as
`terminationGracePeriodSeconds` keep an explicit `0`. For optional pointer
fields, presence itself is meaningful in both directions: a container
`runAsGroup: 0` overrides the pod's `runAsGroup` while an absent one inherits
it, and an empty `namespaceSelector` selects all namespaces while an absent one
selects only the pod's own. A pointer field the bundle sets (even to `0`,
`false` or `{}`) must be present live, and a live pointer field the bundle omits
is drift unless it holds the server default. These include the
`securityContext` fields (`runAsUser`, `runAsGroup`, `runAsNonRoot`,
`fsGroup`, `allowPrivilegeEscalation`, `readOnlyRootFilesystem`,
`privileged` and others), `optional`, `defaultMode`, `mode`,
`tolerationSeconds`, `activeDeadlineSeconds`, `labelSelector` and
`namespaceSelector`. Plain fields that the server omits when zero, such as
`minReadySeconds` or a volume mount's `readOnly`, keep the zero-value
tolerance. An explicit JSON `null` for a struct field means unset, exactly as
if the bundle omitted it: the API server defaults it (a `null`
`terminationGracePeriodSeconds` becomes 30), so the omitted-field rules apply.
A `null` value inside a map field still counts as a key. Anything else the bundle omits,
such as an injected init container or sidecar, extra `env`, a `nodeSelector`
or an extra pod-template annotation (including `kubectl rollout restart`'s
`restartedAt`), is drift and not ready. Map fields (pod-template labels and
annotations, `matchLabels`, `nodeSelector`, resource `limits`/`requests`,
`overhead`) must hold exactly the bundle's keys: an extra key is drift even
when its value is empty, because for example `nodeSelector:
{"example.com/pool": ""}` still requires that node label. A field the server omits matches a
zero/empty bundle value, and Kubernetes quantities (resource
`limits`/`requests`, `overhead` and emptyDir `sizeLimit`) compare by value
(`0.1`, `"0.1"` and `100m` are equal; `1.5Gi` equals the server's `1536Mi`;
`1E` equals `1000P`; `1G` does not equal `1Gi`). Surrounding whitespace is
trimmed as Go's `strings.TrimSpace` does before the server parses a quantity
(`" 64Mi "` is stored, and accepted, as `64Mi`). Every quantity is compared
after the API server's parse-time round-up (away from zero) to nano precision
(an emptyDir `sizeLimit` of `0.1n` is stored, and accepted, as `1n`), and a
binary-suffixed (`Ki`…`Ei`) quantity is then capped at 2^63−1 as the server
does (`8Ei` is stored, and accepted, as `9223372036854775807`; decimal `10E`
is not capped).
Resource `limits`/`requests` and `overhead` are further compared after the
server's round-up to milli precision (a bundle request of `10.1m` is stored,
and accepted, as `11m`); emptyDir `sizeLimit` gets no milli round-up. Any other
live value, including the unrounded text, is drift.
An omitted, `null` or empty container `imagePullPolicy` is applied as the
default the API server derives from that container's image: `Always` for a
`latest` tag (`repo:latest@sha256:…`), otherwise `IfNotPresent` (a digest-pinned
image without a tag). The helper sends it explicitly because the server keeps a
previously defaulted policy when only the image changes. Any other live policy
is drift; an explicit policy is applied and compared as given. Other fields compare as text.
Map keys are always literal: an annotation key such as
`example.com/setting.emptyDir.sizeLimit` compares as text, and drift messages
quote map keys (`spec.template.metadata.annotations["example.com/x"]`, with
`"` and `%` percent-encoded) so a key is never mistaken for a field.
Omitting `replicas` leaves scaling to, for example, a HorizontalPodAutoscaler.
A pre-existing foreign object with the right
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
- The consumed ConfigMap is published immutable under its content-addressed
  name, the Pod template references that name, no ConfigMap exists under the
  bundle name, and the API server refuses another manager's data change to
  it.
- A configuration-only update of a ConfigMap consumed through `envFrom`,
  `configMapKeyRef` and a `subPath` mount starts a new rollout, and every
  running Pod reports the new value through all three when `ready` succeeds.
  Rolling back restores the old value in the Pods the same way. Re-applying
  the same bundle starts no rollout. A Deployment consuming a ConfigMap
  outside its bundle is rejected offline.
- A partially applied update: a test admission policy denies the Deployment
  write after the new ConfigMap is written. The old ConfigMap and template
  remain, and a replacement Pod started from the old template still reads
  the old value through all three paths. The rollback prunes the new
  ConfigMap, needs no new rollout, and every live Pod reports the old value
  once `ready` succeeds. The kubelet's by-name cache lag itself cannot be
  forced in a real cluster; the offline tests model a cache that never
  refreshes through updates, a partial update and rollbacks.
- A stable ConfigMap name with the reserved content-addressed suffix and a
  bundle with more than 9 ConfigMaps are rejected offline. An owned mutable
  ConfigMap at a content-addressed name stops the apply before any write.
- Quota boundary: with `kube-root-ca.crt` and one other ConfigMap present,
  changing every ConfigMap of a 9-ConfigMap bundle peaks exactly at the
  shipped quota of 20 and converges. A partial apply at that bundle size
  that updates one Deployment and fails at the next fills the quota (a
  further create fails with `exceeded quota`). A further bundle is then
  refused before any write and leaves every ConfigMap and the template as
  they were; the rollback to the last fully applied bundle creates nothing,
  prunes the leftovers and converges, and the further bundle then applies.
  An update whose prune deletes two of the previous bundle's ConfigMaps and
  then fails (a test admission policy denies the third delete) exits 2 and
  is not ready. A bundle other than the two the namespace holds is refused
  before any write. After the policy is lifted, the rollback recreates the
  two ConfigMaps at exactly the shipped quota, prunes the update's
  ConfigMaps and converges, and the update then applies.
  An update that renames the Deployment prunes the previous bundle's
  ConfigMaps while a finalizer holds the old Deployment's Pod, so the old
  Deployment stays terminating and the update is not ready. A third bundle
  is refused before any write; the rollback recreates the previous bundle's
  ConfigMaps at exactly the shipped quota and exits 2 while the old
  Deployment is being deleted. After the Pod is released and that deletion
  completes, a third bundle is still refused, and the rollback converges and
  prunes the renamed Deployment.
- A stable-name ConfigMap made immutable out of band, and a content-addressed
  ConfigMap recreated with other content, each stop the apply before any
  write; readiness rejects the forged one, and the apply recreates both after
  an administrator deletes them.
- Readiness of a ConfigMap-only bundle fails while the object is missing, is a
  pre-existing foreign object with matching data, belongs to another
  component or comes from a stale bundle digest, and succeeds once applied.
- Ownership labels are set, the field manager is single, and the creation's
  ownership was transferred to server-side apply.
- The helper does not adopt an existing object, and does not override a field
  owned by another manager. Recovery works after that manager relinquishes the
  field.
- Another manager's pod-template edit that keeps SuiFleet's labels and
  annotations rolls out healthily but is not ready, `apply` reports the
  conflict, and readiness succeeds after that manager relinquishes the field
  and the bundle's template rolls out again.
- Another manager adding the empty-valued `nodeSelector`
  `{"example.com/pool": ""}` rolls out healthily on a node labelled to match
  but is not ready. Readiness succeeds after that manager relinquishes it.
- An explicit container `runAsGroup: 0` is kept by the server and is ready. In
  a bundle whose container inherits the pod's `runAsGroup`, another manager
  adding container `runAsGroup: 0` rolls out healthily but is not ready.
  Readiness succeeds after that manager relinquishes it.
- An explicit `null` `terminationGracePeriodSeconds` is defaulted to 30 by
  the server and is ready. Another manager forcing it to 5 rolls out but is
  not ready; after that manager relinquishes it, apply restores the default
  and readiness succeeds.
- A kubectl wrapper injects administrator actions between the helper's
  ownership check and its write. A competing creation, a replacement by a
  customer object and a relabel are each refused (exit code 3) and left
  unchanged. Pruning leaves a prune candidate alone when it is relabelled or
  replaced between the listing and the delete, including by a replacement
  that carries this component's labels: the racing delete never follows the
  listed UID to the replacement. A same-owner replacement that survives is a
  stale object of this component, which readiness reports not yet pruned
  until a later apply prunes it.
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
