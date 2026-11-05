"use client";

import type { ReactNode } from "react";
import { MANIFEST } from "@/lib/deployment-manifest";
import { ADDRESS_FORMAT, ADDRESS_LINE, COMMANDS, PLATFORMS, guideUrl, type GuideSection } from "@/lib/device-client";

const LINK = "font-medium text-blue-700 underline underline-offset-2 hover:text-blue-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700";

export function GuideLink({ section, children }: { section?: GuideSection; children: ReactNode }) {
  return <a className={LINK} href={guideUrl(section)} target="_blank" rel="noopener noreferrer">{children}</a>;
}

function Commands({ lines }: { lines: string[] }) {
  return <pre className="overflow-x-auto rounded bg-gray-900 px-3 py-2 font-mono text-xs leading-relaxed text-gray-100">{lines.join("\n")}</pre>;
}

/** The manifest this console was built with, for /etc/suifleet/deployment.json. */
function ManifestDownload() {
  const raw = process.env.NEXT_PUBLIC_DEPLOYMENT_MANIFEST;
  if (!MANIFEST || !raw) return null;
  return <button type="button" className="rounded-md border border-gray-300 bg-white px-3 py-1.5 font-medium text-gray-800 hover:bg-gray-50" onClick={() => {
    const url = URL.createObjectURL(new Blob([raw], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "deployment.json";
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }}>Download connection settings</button>;
}

/**
 * Where the device client comes from and what it needs (SF-169).
 * "address": register by a device address the client printed.
 * "enroll": the device enrolls itself with downloaded credential media.
 */
export function DeviceClientHelp({ variant }: { variant: "address" | "enroll" }) {
  return <div data-testid="device-client-help" className="space-y-2 rounded-md border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-800">
    <p>
      The device runs the SuiFleet client, <code>suifleet</code>. See <GuideLink>Install the device client</GuideLink> for
      the supported installation options. Your device installer or integrator uses the <GuideLink section="manifest">connection settings</GuideLink> to connect it to this workspace.
    </p>
    <details><summary className="cursor-pointer font-medium">Installation options</summary>
    <ul className="mt-2 list-disc space-y-0.5 pl-5">
      {PLATFORMS.map((platform) => <li key={platform.section}><GuideLink section={platform.section}>{platform.label}</GuideLink>: {platform.note}</li>)}
    </ul></details>
    <details><summary className="cursor-pointer font-medium">Manual setup for device integrators</summary>
    <div className="mt-2 space-y-2">
    <p>These commands run on the device, not in your browser. They configure its identity and connection; they do not install an application update.</p>
    {variant === "address" ? <>
      <p>
        When it creates the device key, the client prints one line, <code>{ADDRESS_LINE}</code>. The address is {ADDRESS_FORMAT}.
        It is the device’s own public address, not your wallet address. Read it again on the device with:
      </p>
      <Commands lines={[COMMANDS.readAddress, COMMANDS.provisionLog, COMMANDS.deriveAddress]} />
      <p>
        After registering, install <GuideLink section="manifest">deployment.json</GuideLink>, then the DeviceCap ID in <code>/etc/suifleet/signer.toml</code> (<GuideLink section="signer">signing service</GuideLink>, Linux),
        then both IDs in <code>/etc/suifleet/config.toml</code> last, and check it with <code>{COMMANDS.checkConfig}</code> (<GuideLink section="register">steps</GuideLink>).
      </p>
    </> : <>
      <p>After first boot, the device enrolls itself with its credential media (no address to copy):</p>
      <Commands lines={[COMMANDS.enroll]} />
      <p>
        It prints the new <code>device_object_id</code> and <code>device_cap_id</code>. On Linux, write the cap ID into <code>/etc/suifleet/signer.toml</code> first,
        then both into <code>/etc/suifleet/config.toml</code> to start the client (<GuideLink section="enroll">first-boot enrollment</GuideLink>,{" "}
        <GuideLink section="signer">signing service</GuideLink>, <GuideLink section="config">configuration file</GuideLink>).
      </p>
    </>}
    </div></details>
    <p>For a single-device pilot, copy the public device address supplied by the installer. For a batch, give each device only its own enrollment credential. Never enter the device’s private key in the console.</p>
    <ManifestDownload />
  </div>;
}
