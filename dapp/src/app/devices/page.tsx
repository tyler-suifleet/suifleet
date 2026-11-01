"use client";

import { deviceCapacityMessage, registrationFailure } from "@/lib/customer-entitlements";
import { Fragment, useEffect, useState } from "react";
import { useCurrentAccount } from "@/hooks/useCurrentAccount";
import { useCurrentClient } from "@mysten/dapp-kit-react";
import { useQuery } from "@tanstack/react-query";
import { normalizeSuiAddress } from "@mysten/sui/utils";
import { buildIndexGroupTx, existingInboxes, inboxSupported, LISTING_BATCH, unlistedMembers } from "@/lib/fleet-inbox";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { useDeployments } from "@/hooks/useDeployments";
import { useDesiredStates } from "@/hooks/useDesiredStates";
import { useFleetHealth } from "@/hooks/useFleetHealth";
import { usePendingRows } from "@/hooks/usePendingRows";
import { eventIds } from "@/lib/pending-rows";
import { moveType } from "@/lib/deployment-manifest";
import { GroupCreatedBcs } from "@/lib/move-bcs";
import { lastSeenLabel } from "@/lib/telemetry";
import { buildDesiredRevisionTx, desiredConflicts } from "@/lib/desired-state";
import {
  buildRegisterDeviceTx, buildActivateDeviceTx, buildDeregisterDeviceTx,
  buildCreateGroupTx, buildAddDeviceToGroupTx, buildRemoveDeviceFromGroupTx, buildDeleteGroupTx,
} from "@/lib/sui";
import Link from "next/link";
import { buildDeregisterManagedDeviceTx } from "@/lib/customer-fleets";
import { useFleetSelection } from "@/hooks/useFleetSelection";
import { useFleetDataConfig } from "@/hooks/useFleetDataConfig";
import { HealthDetails, TelemetrySettings } from "@/components/DeviceHealth";
import { DeviceUpdateHistory } from "@/components/DeviceUpdateHistory";
import { BulkGroupMembership } from "@/components/BulkGroupMembership";
import { filterDevices, resolveDeviceList, type ListResolution } from "@/lib/group-batch";
import { PageHeader, HealthBadge } from "@/components/ui";
import { DeviceClientHelp } from "@/components/DeviceClientHelp";
import type { DeviceRef } from "@/lib/sui";

type Tab = "devices" | "groups";

export default function DevicesPage() {
  const [tab, setTab] = useState<Tab>("devices");
  return (
    <div className="space-y-4 max-w-5xl">
      <PageHeader title="Devices" subtitle="Manage devices and device groups" />
      <div className="flex gap-0 border-b border-gray-200">
        {(["devices", "groups"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm capitalize border-b-2 -mb-px transition-colors ${
              tab === t ? "border-[#4DA2FF] text-[#4DA2FF] font-medium" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {t === "groups" ? "Device groups" : "Devices"}
          </button>
        ))}
      </div>
      {tab === "devices" ? <DevicesTab /> : <GroupsTab />}
    </div>
  );
}

// ── Devices tab ───────────────────────────────────────────────────────────────

function DevicesTab() {
  const client = useCurrentClient();
  const { fleet } = useFleetSelection();
  const dataConfig = useFleetDataConfig();
  const account = useCurrentAccount();
  const { data: devices = [], isLoading, isSuccess, refetch } = useDevices();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();
  // Keyed by device address: registration knows it before the object ID exists.
  const pending = usePendingRows("devices", isSuccess ? devices.map((d) => d.deviceAddress) : undefined, refetch);
  const adding = pending.waiting.filter((row) => row.kind === "create");
  // A registration not listed yet blocks another: the contract does not reject a second device for the same address.
  const registering = isPending || adding.length > 0;
  const removing = new Set(pending.waiting.filter((row) => row.kind === "delete").map((row) => normalizeSuiAddress(row.id)));
  const [showModal, setShowModal] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [technical, setTechnical] = useState(false);
  const groups = useGroups(), deployments = useDeployments();
  // Bulk group membership (SF-171): select by filter, search or a pasted list.
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [pasting, setPasting] = useState(false);
  const [pasted, setPasted] = useState<ListResolution | null>(null);
  const [bulk, setBulk] = useState(false);
  const shown = filterDevices(devices, query, groups.data ?? []);
  const toggle = (ids: string[], on: boolean) => setSelected((current) => {
    const next = new Set(current);
    for (const id of ids) if (on) next.add(id); else next.delete(id);
    return next;
  });
  // Selection never includes devices that left the fleet.
  const selectedIds = devices.filter((d) => selected.has(d.objectId)).map((d) => d.objectId);
  const [linkedDevice, setLinkedDevice] = useState<string | null>(null);
  useEffect(() => {
    const select = () => { const id = new URLSearchParams(window.location.search).get("device"); setLinkedDevice(id); setExpanded(id); };
    select(); window.addEventListener("popstate", select);
    return () => window.removeEventListener("popstate", select);
  }, []);
  const { data: health = {} } = useFleetHealth(devices.filter((d) => (d.ownerCapId || d.managed)).map((d) => d.objectId));

  function handleRegister(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const name = fd.get("name") as string, deviceAddress = fd.get("deviceAddress") as string;
    if (registering) return;
    const unlisted = [...pending.waiting, ...pending.stale].find((row) => row.kind === "create" && normalizeSuiAddress(row.id) === normalizeSuiAddress(deviceAddress));
    if (unlisted) { alert(`“${unlisted.label}” is already registered with this address but is not listed yet. Refresh the page in a minute before registering it again.`); return; }
    const tx = buildRegisterDeviceTx(
      name,
      deviceAddress,
      fd.get("arch") as string,
      account!.address, fleet,
    );
    signAndExecute(
      { transaction: tx },
      {
        onSuccess: () => { setShowModal(false); pending.add([{ id: deviceAddress, label: name, kind: "create" }]); },
        onError: (err) => alert(`Registration failed: ${registrationFailure(err.message)}`),
      }
    );
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to manage devices.</p>;

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <TelemetrySettings configured={!!dataConfig} />
        <div className="flex items-center gap-3 shrink-0">
          <label className="flex items-center gap-1 text-xs text-gray-500">
            <input type="checkbox" checked={technical} onChange={(e) => setTechnical(e.target.checked)} /> Technical details
          </label>
          <button onClick={() => setShowModal(true)} disabled={registering} className="text-xs text-gray-500 hover:underline disabled:opacity-50">Register by address</button>
          <Link href="/enrollment" className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded">Add devices</Link>
        </div>
      </div>

      {fleet && <p role="status" className="text-sm">{fleet.activeDevices.toString()} active devices / {fleet.deviceLimit.toString()} allowed across this customer’s fleets. {fleet.activeDevices >= fleet.deviceLimit && deviceCapacityMessage(fleet.activeDevices, fleet.deviceLimit)} <Link href="/entitlements" className="underline">Device entitlements</Link></p>}
      {devices.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <input type="search" aria-label="Filter devices" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by name, serial, architecture or group" className="border border-gray-300 rounded px-3 py-1.5 w-72" />
          <button onClick={() => toggle(shown.map((d) => d.objectId), true)} className="text-xs text-[#4DA2FF] hover:underline">Select {shown.length} shown</button>
          <button onClick={() => setSelected(new Set())} disabled={!selectedIds.length} className="text-xs text-gray-500 hover:underline disabled:opacity-40">Clear selection</button>
          <button onClick={() => setPasting(!pasting)} className="text-xs text-gray-500 hover:underline">Paste a list</button>
          <span className="text-xs text-gray-500">{selectedIds.length} selected</span>
          <button onClick={() => setBulk(true)} disabled={!selectedIds.length || !groups.data}
            className="ml-auto border border-[#4DA2FF] text-[#4DA2FF] hover:bg-blue-50 text-sm px-3 py-1.5 rounded disabled:opacity-40">Change groups…</button>
        </div>
      )}
      {pasting && (
        <form className="space-y-2 text-sm" onSubmit={(e) => {
          e.preventDefault();
          const result = resolveDeviceList(new FormData(e.currentTarget).get("list") as string, devices);
          setPasted(result); toggle(result.matched.map((d) => d.objectId), true);
        }}>
          <textarea name="list" aria-label="Serials or names" rows={4} placeholder="One serial, name or object ID per line"
            className="w-full border border-gray-300 rounded px-3 py-2 font-mono text-xs" />
          <button type="submit" className="text-xs text-[#4DA2FF] hover:underline">Select listed devices</button>
          {pasted && (
            <p role="status" className="text-xs text-gray-600">
              Selected {pasted.matched.length} listed device(s).
              {pasted.unmatched.length > 0 && <> Not found: {pasted.unmatched.join(", ")}.</>}
              {pasted.ambiguous.length > 0 && <> Matches several devices, not selected: {pasted.ambiguous.join(", ")}.</>}
            </p>
          )}
        </form>
      )}
      {linkedDevice && !isLoading && !devices.some(device => device.objectId === linkedDevice) && <p role="alert">This device is unavailable in your fleet. Its history may still be visible on Deployments.</p>}
      {pending.stale.map((row) => (
        <p key={row.id} role="status" className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded px-4 py-2">
          {row.kind === "create" ? `“${row.label}” was registered but is not listed yet.` : `“${row.label}” was removed but is still listed.`} Refresh the page in a minute before repeating this action.{" "}
          <button className="underline" onClick={() => pending.dismiss(row.id)}>Dismiss</button>
        </p>
      ))}
      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : devices.length === 0 && adding.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No core devices registered.</p>
            <Link href="/enrollment" className="mt-2 inline-block text-xs text-[#4DA2FF] hover:underline">Enroll your first devices</Link>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                <th className="py-2 pl-4"><input type="checkbox" aria-label="Select all shown"
                  checked={shown.length > 0 && shown.every((d) => selected.has(d.objectId))}
                  onChange={(e) => toggle(shown.map((d) => d.objectId), e.target.checked)} /></th>
                {["Status", "Name", "Serial", "Architecture", "Last seen", ...(technical ? ["Device address", "Object ID"] : []), "Actions"].map((h) => (
                  <th key={h} className="py-2 px-4 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {adding.map((row) => (
                <tr key={row.id} className="bg-blue-50/50" aria-busy="true">
                  <td className="py-3 pl-4" />
                  <td className="py-3 px-4 text-xs text-gray-600" role="status">Registered · adding to the list…</td>
                  <td colSpan={technical ? 7 : 5} className="py-3 pr-4 font-medium">{row.label}</td>
                </tr>
              ))}
              {devices.length > 0 && shown.length === 0 && <tr><td colSpan={technical ? 9 : 7} className="p-6 text-center text-sm text-gray-400">No devices match the filter.</td></tr>}
              {shown.map((d) => (
                <Fragment key={d.objectId}>
                <tr className="hover:bg-gray-50" aria-busy={removing.has(normalizeSuiAddress(d.deviceAddress)) || undefined}>
                  <td className="py-3 pl-4"><input type="checkbox" aria-label={`Select ${d.name}`} checked={selected.has(d.objectId)}
                    onChange={(e) => toggle([d.objectId], e.target.checked)} /></td>
                  <td className="py-3 px-4">
                    {(d.ownerCapId || d.managed) ? (
                      <button onClick={() => setExpanded(expanded === d.objectId ? null : d.objectId)} className="text-left" title="Show installed components and health">
                        <HealthBadge health={health[d.objectId]} />
                      </button>
                    ) : (
                      <span className="text-xs text-amber-600" title="Registered before device identities: its reports are rejected until you activate it.">
                        Not activated
                      </span>
                    )}
                  </td>
                  <td className="py-3 pr-4">
                    <a href={`/devices?device=${encodeURIComponent(d.objectId)}`} onClick={event => {
                      if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                      event.preventDefault(); setExpanded(d.objectId); setLinkedDevice(d.objectId);
                      window.history.pushState(null, "", `?device=${encodeURIComponent(d.objectId)}`);
                    }} className="font-medium text-blue-700 underline" title={d.chainName !== d.name ? `Registered as ${d.chainName}` : undefined}>{d.name}</a>
                    <p className="mt-1 text-xs text-gray-500">{health[d.objectId]?.components.length
                      ? `Last reported: ${health[d.objectId].components.map(component => `${component.name} ${component.version} (${component.installed ? "installed" : "not confirmed"})${component.supervised ? ` (${component.phase ?? "not started"})` : " (one-shot)"}`).join(", ")}`
                      : "Installed versions not reported"}</p>
                    <p className="mt-1 text-xs text-gray-500">Groups: {groups.error || !groups.data ? "Membership unavailable" : groups.data.filter(group => group.deviceIds.includes(d.objectId)).map(group => group.name).join(", ") || "No group"}</p>
                  </td>
                  <td className="py-3 pr-4 text-gray-500">{d.serial ?? "—"}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.arch}</td>
                  <td className="py-3 pr-4 text-xs text-gray-500">{(d.ownerCapId || d.managed) ? lastSeenLabel(health[d.objectId]) : "—"}</td>
                  {technical && <>
                    <td className="py-3 pr-4 font-mono text-xs text-gray-400 max-w-[160px] truncate">{d.deviceAddress}</td>
                    <td className="py-3 pr-4 font-mono text-xs text-gray-400">{d.objectId.slice(0, 14)}…</td>
                  </>}
                  <td className="py-3 flex items-center gap-3">
                    {removing.has(normalizeSuiAddress(d.deviceAddress)) ? <span role="status" className="text-xs text-gray-600">Removed · updating the list…</span> : <>
                    {!d.ownerCapId && !d.managed && (
                      <button
                        disabled={isPending}
                        onClick={() => {
                          signAndExecute(
                            { transaction: buildActivateDeviceTx(d.objectId, account!.address) },
                            {
                              onSuccess: () => setTimeout(() => refetch(), 3000),
                              onError: (err) => alert(`Failed: ${err.message}`),
                            }
                          );
                        }}
                        className="text-xs text-[#4DA2FF] hover:underline disabled:opacity-40"
                      >
                        Activate
                      </button>
                    )}
                    <button
                      disabled={isPending}
                      onClick={async () => {
                        if (!confirm(`Remove "${d.name}"? This cannot be undone.`)) return;
                        signAndExecute(
                          { transaction: fleet ? await buildDeregisterManagedDeviceTx(client, fleet, d.objectId) : buildDeregisterDeviceTx(d) },
                          {
                            onSuccess: () => pending.add([{ id: d.deviceAddress, label: d.name, kind: "delete" }]),
                            onError: (err) => alert(`Failed: ${err.message}`),
                          }
                        );
                      }}
                      className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                    >
                      Remove
                    </button>
                    </>}
                  </td>
                </tr>
                {expanded === d.objectId && (
                  <tr className="bg-gray-50" data-testid="health-details">
                    <td colSpan={technical ? 9 : 7} className="px-4 py-3 space-y-4">
                      <h2 className="font-semibold">{d.name} — device details</h2>
                      <p className="text-sm">Current groups: {groups.error || !groups.data ? "Membership unavailable" : groups.data.filter(group => group.deviceIds.includes(d.objectId)).map(group => group.name).join(", ") || "No group"}</p>
                      <HealthDetails health={health[d.objectId]} />
                      <h3 className="font-semibold">Update history</h3>
                      <DeviceUpdateHistory deviceId={d.objectId} devices={devices} groups={groups.error ? undefined : groups.data}
                        deployments={deployments.data ?? []} loading={deployments.isLoading} error={deployments.error?.message} />
                      <Link href={`/deployments?device=${encodeURIComponent(d.objectId)}`} className="inline-block text-sm text-blue-700 underline">View all updates for {d.name}</Link>
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {bulk && groups.data && (
        <BulkGroupMembership devices={devices} groups={groups.data} selected={selectedIds} fleet={fleet}
          reloadGroups={() => groups.refetch()}
          onClose={() => { setBulk(false); void groups.refetch(); }} />
      )}

      {showModal && (
        <Modal title="Register core device by address" onClose={() => setShowModal(false)}>
          <form onSubmit={handleRegister} className="space-y-4">
            <p className="text-xs text-gray-500">
              Advanced. To add devices without copying addresses, use <Link href="/enrollment" className="text-[#4DA2FF] hover:underline">Enrollment</Link>:
              reserve names and serials, and each device enrolls with its own key.
            </p>
            <DeviceClientHelp variant="address" />
            <FormField label="Device name" name="name" placeholder="rpi-001" required />
            <FormField label="Device SUI address" name="deviceAddress" placeholder="0x…" required />
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Architecture</label>
              <select name="arch" required className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#4DA2FF]">
                <option value="aarch64">aarch64 (ARM 64-bit)</option>
                <option value="armv7">armv7 (ARM 32-bit)</option>
                <option value="x86_64">x86_64</option>
                <option value="x86">x86 (32-bit i686)</option>
                <option value="riscv64">riscv64</option>
              </select>
            </div>
            <ModalButtons isPending={registering || !!fleet && fleet.activeDevices >= fleet.deviceLimit} label="Register" onCancel={() => setShowModal(false)} />
          </form>
        </Modal>
      )}
    </>
  );
}

// ── Groups tab ────────────────────────────────────────────────────────────────

function GroupsTab() {
  const { fleet } = useFleetSelection();
  const account = useCurrentAccount();
  const client = useCurrentClient();
  const { data: groups = [], isLoading, isSuccess, refetch } = useGroups();
  const { data: devices = [] } = useDevices();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();
  const pending = usePendingRows("groups", isSuccess ? groups.map((g) => g.objectId) : undefined, refetch);
  const deleting = new Set(pending.waiting.filter((row) => row.kind === "delete").map((row) => row.id));
  const creating = pending.waiting.some((row) => row.kind === "create");
  const [showCreate, setShowCreate] = useState(false);
  const [managingGroup, setManagingGroup] = useState<string | null>(null);
  const { data: desired = {}, refetch: refetchDesired } = useDesiredStates(groups.map((g) => g.objectId));
  const { data: deployments = [] } = useDeployments();
  const conflicts = desiredConflicts(groups, desired);
  const managedGroup = groups.find((g) => g.objectId === managingGroup);
  // Operated members whose inbox does not list the managed group yet: the
  // index migration lists them in batches until none remain.
  const operatedMembers = managedGroup ? devices.filter((d) => d.ownerCapId && managedGroup.deviceIds.includes(d.objectId))
    .map((d) => ({ deviceId: d.objectId, ownerCapId: d.ownerCapId! })) : [];
  const { data: unlisted = [], refetch: refetchUnlisted } = useQuery({
    queryKey: ["unlisted-members", managedGroup?.objectId, operatedMembers.map((m) => m.deviceId)],
    queryFn: () => unlistedMembers(client, managedGroup!.objectId, operatedMembers),
    enabled: inboxSupported() && !!managedGroup && operatedMembers.length > 0,
  });
  const label = (deploymentId: string) => {
    const deployment = deployments.find((d) => d.deploymentId === deploymentId);
    return deployment ? `v${deployment.version}` : `${deploymentId.slice(0, 10)}… (deleted or not yours)`;
  };

  function handleStopDesiring(groupId: string, component: string) {
    if (!confirm(`Stop desiring "${component}" in this group? Devices retire it from management; it is not uninstalled.`)) return;
    signAndExecute(
      { transaction: buildDesiredRevisionTx([groupId], undefined, [component], fleet) },
      {
        onSuccess: () => { setTimeout(() => refetchDesired(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  function handleCreateGroup(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const name = fd.get("name") as string;
    signAndExecute(
      { transaction: buildCreateGroupTx(name, fd.get("description") as string, fleet) },
      {
        onSuccess: (result) => {
          setShowCreate(false);
          const ids = eventIds(result.events, moveType("device_group::GroupCreated"), (bytes) => GroupCreatedBcs.parse(bytes).group_id);
          if (ids.length) pending.add(ids.map((id) => ({ id, label: name, kind: "create" as const })));
          else void refetch();
        },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  /** Whether each device's inbox exists, when this deployment has inboxes. */
  async function inboxes(deviceIds: string[]) {
    return !fleet && inboxSupported() ? await existingInboxes(client, deviceIds) : undefined;
  }

  async function handleAddDevice(groupId: string, device: DeviceRef) {
    let known;
    try { known = await inboxes([device.objectId]); } catch (err) { alert(`Failed: ${err instanceof Error ? err.message : err}`); return; }
    signAndExecute(
      { transaction: buildAddDeviceToGroupTx(groupId, device, known?.has(device.objectId), fleet) },
      {
        onSuccess: () => { setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  async function handleRemoveDevice(groupId: string, deviceId: string) {
    let known;
    try { known = await inboxes([deviceId]); } catch (err) { alert(`Failed: ${err instanceof Error ? err.message : err}`); return; }
    signAndExecute(
      { transaction: buildRemoveDeviceFromGroupTx(groupId, deviceId, known?.has(deviceId), fleet) },
      {
        onSuccess: () => { setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  /**
   * One batch of the member-index migration: moves unindexed members into
   * the index and lists the group in operated members' inboxes that do not
   * list it yet (read fresh, so repeated batches progress over every member).
   */
  async function handleIndex(groupId: string, unindexed: number) {
    let pending;
    try { pending = await unlistedMembers(client, groupId, operatedMembers); } catch (err) { alert(`Failed: ${err instanceof Error ? err.message : err}`); return; }
    if (unindexed === 0 && pending.length === 0) { void refetchUnlisted(); return; }
    signAndExecute(
      { transaction: buildIndexGroupTx(groupId, pending.slice(0, LISTING_BATCH), unindexed > 0) },
      {
        onSuccess: () => { setTimeout(() => { void refetch(); void refetchUnlisted(); }, 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet.</p>;

  return (
    <>
      <div className="flex justify-end">
        <button onClick={() => setShowCreate(true)} disabled={isPending || creating} className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded disabled:opacity-50">
          {creating ? "Adding group…" : "Create group"}
        </button>
      </div>
      {pending.stale.map((row) => (
        <p key={row.id} role="status" className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded px-4 py-2">
          {row.kind === "create" ? `Group “${row.label}” was created but is not listed yet.` : `Group “${row.label}” was deleted but is still listed.`} Refresh the page in a minute before repeating this action.{" "}
          <button className="underline" onClick={() => pending.dismiss(row.id)}>Dismiss</button>
        </p>
      ))}

      {conflicts.length > 0 && (
        <div className="text-sm text-amber-800 bg-amber-50 border border-amber-200 rounded px-4 py-3 space-y-1">
          <p className="font-medium">Groups disagree on desired components. Affected devices keep what they have installed until the groups agree:</p>
          {conflicts.map((c) => (
            <p key={`${c.deviceId}-${c.component}`} className="text-xs">
              {devices.find((d) => d.objectId === c.deviceId)?.name ?? c.deviceId.slice(0, 14) + "…"}: {c.component} —{" "}
              {Object.entries(c.choices).map(([group, deployment]) =>
                `${groups.find((g) => g.objectId === group)?.name ?? group.slice(0, 10)} wants ${label(deployment)}`).join("; ")}
            </p>
          ))}
        </div>
      )}

      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : groups.length === 0 && !creating ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No device groups yet.</p>
            <button onClick={() => setShowCreate(true)} className="mt-2 text-xs text-[#4DA2FF] hover:underline">Create your first group</button>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                {["Group name", "Description", "Devices", "Desired state", "Object ID", "Actions"].map((h) => (
                  <th key={h} className="py-2 px-4 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {pending.waiting.filter((row) => row.kind === "create").map((row) => (
                <tr key={row.id} className="bg-blue-50/50" aria-busy="true">
                  <td className="py-3 px-4 font-medium">{row.label}</td>
                  <td colSpan={5} className="py-3 pr-4 text-xs text-gray-600" role="status">Created · adding to the list…</td>
                </tr>
              ))}
              {groups.map((g) => (
                <tr key={g.objectId} className="hover:bg-gray-50" aria-busy={deleting.has(g.objectId) || undefined}>
                  <td className="py-3 px-4 font-medium">{g.name}</td>
                  <td className="py-3 pr-4 text-gray-500 text-xs">{g.description || "—"}</td>
                  <td className="py-3 pr-4 text-gray-500">{g.deviceIds.length}</td>
                  <td className="py-3 pr-4 text-gray-500 text-xs">
                    {desired[g.objectId]
                      ? `revision ${desired[g.objectId].revision} · ${Object.keys(desired[g.objectId].components).length} component(s)`
                      : "—"}
                  </td>
                  <td className="py-3 pr-4 font-mono text-xs text-gray-400">{g.objectId.slice(0, 14)}…</td>
                  <td className="py-3 flex items-center gap-3">
                    {deleting.has(g.objectId) ? <span role="status" className="text-xs text-gray-600">Deleted · removing from the list…</span> : <>
                    <button onClick={() => setManagingGroup(g.objectId)} className="text-xs text-[#4DA2FF] hover:underline">
                      Manage
                    </button>
                    <button
                      disabled={isPending}
                      onClick={() => {
                        if (!confirm(`Delete group "${g.name}"? This cannot be undone.`)) return;
                        signAndExecute(
                          { transaction: buildDeleteGroupTx(g.objectId, fleet) },
                          {
                            onSuccess: () => pending.add([{ id: g.objectId, label: g.name, kind: "delete" }]),
                            onError: (err) => alert(`Failed: ${err.message}`),
                          }
                        );
                      }}
                      className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                    >
                      Delete
                    </button>
                    </>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showCreate && (
        <Modal title="Create device group" onClose={() => setShowCreate(false)}>
          <form onSubmit={handleCreateGroup} className="space-y-4">
            <FormField label="Group name" name="name" placeholder="production-pi4s" required />
            <FormField label="Description" name="description" placeholder="Optional" />
            <ModalButtons isPending={isPending} label="Create" onCancel={() => setShowCreate(false)} />
          </form>
        </Modal>
      )}

      {managedGroup && (
        <Modal title={`Manage — ${managedGroup.name}`} onClose={() => setManagingGroup(null)}>
          <div className="space-y-4">
            <div>
              <p className="text-xs font-medium text-gray-500 uppercase mb-2">
                Desired state{desired[managedGroup.objectId] ? ` (revision ${desired[managedGroup.objectId].revision})` : ""}
              </p>
              {!desired[managedGroup.objectId] || Object.keys(desired[managedGroup.objectId].components).length === 0 ? (
                <p className="text-sm text-gray-400">None. Deploy firmware with &ldquo;desired version&rdquo; selected to set one.</p>
              ) : (
                <div className="space-y-1">
                  {Object.entries(desired[managedGroup.objectId].components).map(([component, deployment]) => (
                    <div key={component} className="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded text-sm">
                      <span>{component} <span className="text-xs text-gray-500">{label(deployment)}</span></span>
                      <button
                        disabled={isPending}
                        onClick={() => handleStopDesiring(managedGroup.objectId, component)}
                        className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                      >
                        Stop desiring
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {!fleet && inboxSupported() && (managedGroup.unindexed > 0 || unlisted.length > 0) && (
              <div className="text-sm bg-gray-50 border border-gray-200 rounded px-3 py-2 space-y-1">
                {managedGroup.unindexed > 0 && (
                  <p>{managedGroup.unindexed} member(s) are not yet in the group&apos;s member index. Indexing removes the
                    group&apos;s size limit and lets devices find it without event history, but older daemons and tools
                    then no longer see indexed members.</p>
                )}
                {unlisted.length > 0 && (
                  <p>{unlisted.length} device(s) you operate do not list this group in their inbox yet, so they need event
                    history to find it. Each batch lists up to {LISTING_BATCH}.</p>
                )}
                <button disabled={isPending} onClick={() => void handleIndex(managedGroup.objectId, managedGroup.unindexed)}
                  className="text-xs text-[#4DA2FF] hover:underline disabled:opacity-40">
                  {managedGroup.unindexed > 0 ? "Index members" : "List group in inboxes"}
                </button>
              </div>
            )}

            <div>
              <p className="text-xs font-medium text-gray-500 uppercase mb-2">Members</p>
              {managedGroup.deviceIds.length === 0 ? (
                <p className="text-sm text-gray-400">No devices in this group.</p>
              ) : (
                <div className="space-y-1">
                  {managedGroup.deviceIds.map((id) => {
                    const dev = devices.find((d) => d.objectId === id);
                    return (
                      <div key={id} className="flex items-center justify-between py-1.5 px-3 bg-gray-50 rounded text-sm">
                        <span>{dev?.name ?? id.slice(0, 14) + "…"}</span>
                        <button
                          disabled={isPending}
                          onClick={() => handleRemoveDevice(managedGroup.objectId, id)}
                          className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                        >
                          Remove
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>

            <div>
              <p className="text-xs font-medium text-gray-500 uppercase mb-2">Add device</p>
              <p className="text-xs text-gray-500 mb-2">To change many devices at once, select them on the Devices tab and choose Change groups.</p>
              {devices.filter((d) => !managedGroup.deviceIds.includes(d.objectId)).length === 0 ? (
                <p className="text-sm text-gray-400">All devices are already in this group.</p>
              ) : (
                <div className="space-y-1">
                  {devices
                    .filter((d) => !managedGroup.deviceIds.includes(d.objectId))
                    .map((d) => (
                      <div key={d.objectId} className="flex items-center justify-between py-1.5 px-3 border border-gray-200 rounded text-sm">
                        <span>{d.name} <span className="text-xs text-gray-400">{d.arch}</span></span>
                        <button
                          disabled={isPending}
                          onClick={() => handleAddDevice(managedGroup.objectId, d)}
                          className="text-xs text-[#4DA2FF] hover:underline disabled:opacity-40"
                        >
                          Add
                        </button>
                      </div>
                    ))}
                </div>
              )}
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

// ── Shared UI helpers ─────────────────────────────────────────────────────────

function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function FormField({ label, name, placeholder, required }: { label: string; name: string; placeholder?: string; required?: boolean }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">{label}</label>
      <input name={name} placeholder={placeholder} required={required}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#4DA2FF]" />
    </div>
  );
}

function ModalButtons({ isPending, label, onCancel }: { isPending: boolean; label: string; onCancel: () => void }) {
  return (
    <div className="flex gap-2 pt-2">
      <button type="submit" disabled={isPending} className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white px-4 py-2 rounded text-sm disabled:opacity-50">
        {isPending ? `${label}…` : label}
      </button>
      <button type="button" onClick={onCancel} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
    </div>
  );
}
