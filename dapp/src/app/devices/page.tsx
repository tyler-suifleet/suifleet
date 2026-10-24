"use client";

import { Fragment, useEffect, useState } from "react";
import { useCurrentAccount } from "@mysten/dapp-kit-react";
import { useFleetTransaction } from "@/hooks/useFleetTransaction";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import { useDeployments } from "@/hooks/useDeployments";
import { useDesiredStates } from "@/hooks/useDesiredStates";
import { useFleetHealth } from "@/hooks/useFleetHealth";
import { describeServices, lastSeenLabel, type DeviceHealth } from "@/lib/telemetry";
import { buildDesiredRevisionTx, desiredConflicts } from "@/lib/desired-state";
import {
  buildRegisterDeviceTx, buildActivateDeviceTx, buildDeregisterDeviceTx,
  buildCreateGroupTx, buildAddDeviceToGroupTx, buildRemoveDeviceFromGroupTx, buildDeleteGroupTx,
} from "@/lib/sui";
import Link from "next/link";
import { DATA_CONFIG } from "@/lib/fleet-data";
import { DeviceUpdateHistory } from "@/components/DeviceUpdateHistory";
import { PageHeader, HealthBadge } from "@/components/ui";
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
  const account = useCurrentAccount();
  const { data: devices = [], isLoading, refetch } = useDevices();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();
  const [showModal, setShowModal] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [technical, setTechnical] = useState(false);
  const groups = useGroups(), deployments = useDeployments();
  const [linkedDevice, setLinkedDevice] = useState<string | null>(null);
  useEffect(() => {
    const select = () => { const id = new URLSearchParams(window.location.search).get("device"); setLinkedDevice(id); setExpanded(id); };
    select(); window.addEventListener("popstate", select);
    return () => window.removeEventListener("popstate", select);
  }, []);
  const { data: health = {} } = useFleetHealth(devices.filter((d) => d.ownerCapId).map((d) => d.objectId));

  function handleRegister(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const tx = buildRegisterDeviceTx(
      fd.get("name") as string,
      fd.get("deviceAddress") as string,
      fd.get("arch") as string,
      account!.address,
    );
    signAndExecute(
      { transaction: tx },
      {
        onSuccess: () => { setShowModal(false); setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Registration failed: ${err.message}`),
      }
    );
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet to manage devices.</p>;

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <TelemetrySettings />
        <div className="flex items-center gap-3 shrink-0">
          <label className="flex items-center gap-1 text-xs text-gray-500">
            <input type="checkbox" checked={technical} onChange={(e) => setTechnical(e.target.checked)} /> Technical details
          </label>
          <button onClick={() => setShowModal(true)} className="text-xs text-gray-500 hover:underline">Register by address</button>
          <Link href="/enrollment" className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded">Add devices</Link>
        </div>
      </div>

      {linkedDevice && !isLoading && !devices.some(device => device.objectId === linkedDevice) && <p role="alert">This device is unavailable in your fleet. Its history may still be visible on Deployments.</p>}
      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : devices.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No core devices registered.</p>
            <Link href="/enrollment" className="mt-2 inline-block text-xs text-[#4DA2FF] hover:underline">Enroll your first devices</Link>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                {["Status", "Name", "Serial", "Architecture", "Last seen", ...(technical ? ["Device address", "Object ID"] : []), "Actions"].map((h) => (
                  <th key={h} className="py-2 px-4 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {devices.map((d) => (
                <Fragment key={d.objectId}>
                <tr className="hover:bg-gray-50">
                  <td className="py-3 px-4">
                    {d.ownerCapId ? (
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
                  <td className="py-3 pr-4 text-xs text-gray-500">{d.ownerCapId ? lastSeenLabel(health[d.objectId]) : "—"}</td>
                  {technical && <>
                    <td className="py-3 pr-4 font-mono text-xs text-gray-400 max-w-[160px] truncate">{d.deviceAddress}</td>
                    <td className="py-3 pr-4 font-mono text-xs text-gray-400">{d.objectId.slice(0, 14)}…</td>
                  </>}
                  <td className="py-3 flex items-center gap-3">
                    {!d.ownerCapId && (
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
                      onClick={() => {
                        if (!confirm(`Remove "${d.name}"? This cannot be undone.`)) return;
                        signAndExecute(
                          { transaction: buildDeregisterDeviceTx(d) },
                          {
                            onSuccess: () => setTimeout(() => refetch(), 3000),
                            onError: (err) => alert(`Failed: ${err.message}`),
                          }
                        );
                      }}
                      className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                    >
                      Remove
                    </button>
                  </td>
                </tr>
                {expanded === d.objectId && (
                  <tr className="bg-gray-50" data-testid="health-details">
                    <td colSpan={technical ? 8 : 6} className="px-4 py-3 space-y-4">
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

      {showModal && (
        <Modal title="Register core device by address" onClose={() => setShowModal(false)}>
          <form onSubmit={handleRegister} className="space-y-4">
            <p className="text-xs text-gray-500">
              Advanced. To add devices without copying addresses, use <Link href="/enrollment" className="text-[#4DA2FF] hover:underline">Enrollment</Link>:
              reserve names and serials, and each device enrolls with its own key.
            </p>
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
            <ModalButtons isPending={isPending} label="Register" onCancel={() => setShowModal(false)} />
          </form>
        </Modal>
      )}
    </>
  );
}

// ── Groups tab ────────────────────────────────────────────────────────────────

function GroupsTab() {
  const account = useCurrentAccount();
  const { data: groups = [], isLoading, refetch } = useGroups();
  const { data: devices = [] } = useDevices();
  const { mutate: signAndExecute, isPending } = useFleetTransaction();
  const [showCreate, setShowCreate] = useState(false);
  const [managingGroup, setManagingGroup] = useState<string | null>(null);
  const { data: desired = {}, refetch: refetchDesired } = useDesiredStates(groups.map((g) => g.objectId));
  const { data: deployments = [] } = useDeployments();
  const conflicts = desiredConflicts(groups, desired);
  const label = (deploymentId: string) => {
    const deployment = deployments.find((d) => d.deploymentId === deploymentId);
    return deployment ? `v${deployment.version}` : `${deploymentId.slice(0, 10)}… (deleted or not yours)`;
  };

  function handleStopDesiring(groupId: string, component: string) {
    if (!confirm(`Stop desiring "${component}" in this group? Devices retire it from management; it is not uninstalled.`)) return;
    signAndExecute(
      { transaction: buildDesiredRevisionTx([groupId], undefined, [component]) },
      {
        onSuccess: () => { setTimeout(() => refetchDesired(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  function handleCreateGroup(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    signAndExecute(
      { transaction: buildCreateGroupTx(fd.get("name") as string, fd.get("description") as string) },
      {
        onSuccess: () => { setShowCreate(false); setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  function handleAddDevice(groupId: string, device: DeviceRef) {
    signAndExecute(
      { transaction: buildAddDeviceToGroupTx(groupId, device) },
      {
        onSuccess: () => { setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  function handleRemoveDevice(groupId: string, deviceId: string) {
    signAndExecute(
      { transaction: buildRemoveDeviceFromGroupTx(groupId, deviceId) },
      {
        onSuccess: () => { setTimeout(() => refetch(), 3000); },
        onError: (err) => alert(`Failed: ${err.message}`),
      }
    );
  }

  if (!account) return <p className="text-sm text-gray-500">Connect your wallet.</p>;

  const managedGroup = groups.find((g) => g.objectId === managingGroup);

  return (
    <>
      <div className="flex justify-end">
        <button onClick={() => setShowCreate(true)} className="bg-[#4DA2FF] hover:bg-[#2e8ed4] text-white text-sm px-4 py-2 rounded">
          Create group
        </button>
      </div>

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
        ) : groups.length === 0 ? (
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
              {groups.map((g) => (
                <tr key={g.objectId} className="hover:bg-gray-50">
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
                    <button onClick={() => setManagingGroup(g.objectId)} className="text-xs text-[#4DA2FF] hover:underline">
                      Manage
                    </button>
                    <button
                      disabled={isPending}
                      onClick={() => {
                        if (!confirm(`Delete group "${g.name}"? This cannot be undone.`)) return;
                        signAndExecute(
                          { transaction: buildDeleteGroupTx(g.objectId) },
                          {
                            onSuccess: () => setTimeout(() => refetch(), 3000),
                            onError: (err) => alert(`Failed: ${err.message}`),
                          }
                        );
                      }}
                      className="text-xs text-red-500 hover:text-red-700 disabled:opacity-40"
                    >
                      Delete
                    </button>
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

// ── Device health (SF-019) ────────────────────────────────────────────────────

/** Health and archives are verified directly from the fleet's on-chain index. */
function TelemetrySettings() {
  return <p className="text-xs text-gray-500">{DATA_CONFIG ? "Health is verified directly from Sui." : "Configure the on-chain fleet to publish and read device health."} <Link href="/data" className="text-blue-700 underline">Read encrypted logs and diagnostics.</Link></p>;
}

function HealthDetails({ health }: { health?: DeviceHealth }) {
  if (!health) return <p className="text-xs text-gray-400">Loading health…</p>;
  return (
    <div className="space-y-2 text-xs">
      {health.reasons.length > 0 && (
        <ul className="list-disc pl-4 text-amber-800">
          {health.reasons.map((reason, i) => <li key={i}>{reason}</li>)}
        </ul>
      )}
      {health.lastSeenMs !== undefined && (
        <p className="text-gray-500">
          Last report {new Date(health.lastSeenMs).toLocaleString()} (taken {new Date(health.observedMs!).toLocaleString()} by the device clock),
          daemon {health.daemonVersion}. Reports are signed by the device and verified against its on-chain identity.
        </p>
      )}
      {health.services && (
        <div>
          <p className="font-medium text-gray-600">Services</p>
          <ul className="text-gray-600 whitespace-pre-wrap">
            {describeServices(health.services).map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </div>
      )}
      {health.components.length === 0 ? (
        <p className="text-gray-400">No installed components reported.</p>
      ) : (
        <table className="w-full">
          <thead>
            <tr className="text-left text-gray-500">
              {["Component", "Version", "Installed", "Runtime", "Restarts", "Last exit"].map((h) => <th key={h} className="py-1 pr-4 font-medium">{h}</th>)}
            </tr>
          </thead>
          <tbody>
            {health.components.map((c) => (
              <tr key={c.name}>
                <td className="py-1 pr-4 font-medium">{c.name}</td>
                <td className="py-1 pr-4">{c.version}</td>
                <td className="py-1 pr-4">{c.installed ? "yes" : "not confirmed"}</td>
                <td className="py-1 pr-4">{c.supervised ? c.phase ?? "not started" : "one-shot"}</td>
                <td className="py-1 pr-4">{c.restarts ?? "—"}</td>
                <td className="py-1 pr-4 text-gray-500">{c.last_exit ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
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
