"use client";

import { useState } from "react";
import { useCurrentAccount, useSignAndExecuteTransaction } from "@mysten/dapp-kit";
import { useDevices } from "@/hooks/useDevices";
import { useGroups } from "@/hooks/useGroups";
import {
  buildRegisterDeviceTx, buildIssueCapTx,
  buildCreateGroupTx, buildAddDeviceToGroupTx, buildRemoveDeviceFromGroupTx,
} from "@/lib/sui";
import { PageHeader, StatusDot } from "@/app/page";

type Tab = "devices" | "groups";

export default function DevicesPage() {
  const [tab, setTab] = useState<Tab>("devices");
  return (
    <div className="space-y-4 max-w-5xl">
      <PageHeader title="Core devices" subtitle="Manage devices and device groups" />
      <div className="flex gap-0 border-b border-gray-200">
        {(["devices", "groups"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm capitalize border-b-2 -mb-px transition-colors ${
              tab === t ? "border-[#ec7211] text-[#ec7211] font-medium" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {t === "groups" ? "Device groups" : "Core devices"}
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
  const { mutate: signAndExecute, isPending } = useSignAndExecuteTransaction();
  const [showModal, setShowModal] = useState(false);

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
      <div className="flex justify-end">
        <button onClick={() => setShowModal(true)} className="bg-[#ec7211] hover:bg-[#d4620e] text-white text-sm px-4 py-2 rounded">
          Register device
        </button>
      </div>

      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : devices.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No core devices registered.</p>
            <button onClick={() => setShowModal(true)} className="mt-2 text-xs text-[#0073bb] hover:underline">Register your first device</button>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                {["Status", "Name", "Architecture", "Device address", "Object ID", "Actions"].map((h) => (
                  <th key={h} className="py-2 px-4 font-medium">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {devices.map((d) => (
                <tr key={d.objectId} className="hover:bg-gray-50">
                  <td className="py-3 px-4"><StatusDot healthy /></td>
                  <td className="py-3 pr-4 font-medium">{d.name}</td>
                  <td className="py-3 pr-4 text-gray-500">{d.arch}</td>
                  <td className="py-3 pr-4 font-mono text-xs text-gray-400 max-w-[160px] truncate">{d.deviceAddress}</td>
                  <td className="py-3 pr-4 font-mono text-xs text-gray-400">{d.objectId.slice(0, 14)}…</td>
                  <td className="py-3">
                    <button
                      disabled={isPending}
                      onClick={() => {
                        signAndExecute(
                          { transaction: buildIssueCapTx(d.objectId) },
                          {
                            onSuccess: () => alert(`DeviceCap issued to ${d.deviceAddress}`),
                            onError: (err) => alert(`Failed: ${err.message}`),
                          }
                        );
                      }}
                      className="text-xs text-[#0073bb] hover:underline disabled:opacity-40"
                    >
                      Issue cap
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showModal && (
        <Modal title="Register core device" onClose={() => setShowModal(false)}>
          <form onSubmit={handleRegister} className="space-y-4">
            <FormField label="Device name" name="name" placeholder="rpi-001" required />
            <FormField label="Device SUI address" name="deviceAddress" placeholder="0x…" required />
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Architecture</label>
              <select name="arch" required className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#ec7211]">
                <option value="aarch64">aarch64 (ARM 64-bit)</option>
                <option value="armv7">armv7 (ARM 32-bit)</option>
                <option value="x86_64">x86_64</option>
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
  const { mutate: signAndExecute, isPending } = useSignAndExecuteTransaction();
  const [showCreate, setShowCreate] = useState(false);
  const [managingGroup, setManagingGroup] = useState<string | null>(null);

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

  function handleAddDevice(groupId: string, deviceObjectId: string) {
    signAndExecute(
      { transaction: buildAddDeviceToGroupTx(groupId, deviceObjectId) },
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
        <button onClick={() => setShowCreate(true)} className="bg-[#ec7211] hover:bg-[#d4620e] text-white text-sm px-4 py-2 rounded">
          Create group
        </button>
      </div>

      <div className="bg-white border border-gray-200 rounded">
        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">Loading…</div>
        ) : groups.length === 0 ? (
          <div className="p-12 text-center">
            <p className="text-sm text-gray-500">No device groups yet.</p>
            <button onClick={() => setShowCreate(true)} className="mt-2 text-xs text-[#0073bb] hover:underline">Create your first group</button>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-xs text-gray-500 bg-gray-50">
                {["Group name", "Description", "Devices", "Object ID", "Actions"].map((h) => (
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
                  <td className="py-3 pr-4 font-mono text-xs text-gray-400">{g.objectId.slice(0, 14)}…</td>
                  <td className="py-3">
                    <button onClick={() => setManagingGroup(g.objectId)} className="text-xs text-[#0073bb] hover:underline">
                      Manage devices
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
        <Modal title={`Manage devices — ${managedGroup.name}`} onClose={() => setManagingGroup(null)}>
          <div className="space-y-4">
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
                          onClick={() => handleAddDevice(managedGroup.objectId, d.objectId)}
                          className="text-xs text-[#0073bb] hover:underline disabled:opacity-40"
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
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-6">
        <h2 className="text-base font-semibold mb-4">{title}</h2>
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
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#ec7211]" />
    </div>
  );
}

function ModalButtons({ isPending, label, onCancel }: { isPending: boolean; label: string; onCancel: () => void }) {
  return (
    <div className="flex gap-2 pt-2">
      <button type="submit" disabled={isPending} className="bg-[#ec7211] hover:bg-[#d4620e] text-white px-4 py-2 rounded text-sm disabled:opacity-50">
        {isPending ? `${label}…` : label}
      </button>
      <button type="button" onClick={onCancel} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Cancel</button>
    </div>
  );
}
