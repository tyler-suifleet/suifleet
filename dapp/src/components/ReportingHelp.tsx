import Link from "next/link";

export function ReportingHelp() {
  return <details className="rounded border bg-white p-4 text-sm space-y-3">
    <summary className="cursor-pointer font-semibold">Enable logs and understand reporting status</summary>
    <p>Start in <Link className="text-blue-700 underline" href="/settings/#reporting">Fleet settings → Device reporting</Link>: set up reporting, enroll the device, enable its writer and download its reporting settings. Merge those settings on the device without replacing its identity or journals.</p>
    <p>Health reports need a running, configured device and funded activity allowance. Logs are optional: an integrator must also configure device log collection, encrypted archives and the approved encryption/storage integration, and fund storage in <Link className="text-blue-700 underline" href="/budget/">Network budget</Link>. Changing an upload interval alone enables none of these.</p>
    <p>Unreported means no verified sample has arrived; check device configuration, connectivity and funding. Unavailable means the app could not read or verify reporting; check the connection and fleet bindings. A verified sample describes its report time, not continuous live health. Empty archives do not prove a device is healthy.</p>
    <a className="inline-block text-blue-700 underline" href="https://github.com/tyler-suifleet/suifleet/blob/main/docs/SERVERLESS_FLEET_DATA.md#native-deployment" target="_blank" rel="noreferrer">Integrator guide: device log collection and encrypted archive setup</a>
  </details>;
}
