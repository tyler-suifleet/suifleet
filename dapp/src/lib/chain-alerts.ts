// SF-020 alert rules over verified Sui receipts and signed device health.
// An optional relay can consume these same results; its claims are never input.
import type { SuiGrpcClient } from '@mysten/sui/grpc';
import { normalizeSuiAddress as id } from '@mysten/sui/utils';
import { fetchDevices, fetchDeploymentEvents, type DeviceObject, type DeploymentEvent } from './sui';
import { fetchFleetHealth, type DeviceHealth } from './telemetry';
import { DATA_CONFIG, listFleetDeviceIds } from './fleet-data';
import type { AlertsResult, FleetAlert, AlertKind } from './alerts';
export function deriveChainAlerts(devices: Pick<DeviceObject, 'objectId' | 'name'>[], deployments: DeploymentEvent[], health: Record<string, DeviceHealth>, now: number): AlertsResult {
  const items: FleetAlert[] = [];
  for (const d of devices) {
    const h = health[d.objectId];
    if (!h || ['unavailable', 'unverified'].includes(h.status)) return { status: 'unavailable', reason: 'Device health could not be verified' };
    if (h.status === 'unreported') continue;
    const add = (kind: AlertKind, severity: FleetAlert['severity'], subject: string, message: string, action: string, sinceMs?: number) => {
      const a: FleetAlert = { key: `${kind}:${d.objectId}:${subject}`, kind, severity, deviceId: d.objectId, deviceName: d.name, message: message.slice(0,400), action, sinceMs };
      items.push(a); return a;
    };
    const online = h.status === 'healthy' || h.status === 'degraded';
    if (h.status === 'offline') add('device-offline','warning','',`${d.name} has not reported since ${h.lastSeenMs ?? 0} ms`,'Check device power and networking.',h.lastSeenMs);
    if (h.status === 'degraded') {
      for (const c of h.components.filter(c => c.supervised && c.phase === 'broken')) {
        const a = add('component-broken','critical',c.name,`${c.name} ${c.version} exhausted its restart budget: ${c.last_exit ?? 'no exit recorded'}`,'Inspect component logs; deploy a fixed version or roll back.',h.observedMs);
        a.component = c.name; a.deploymentId = c.deployment_id;
      }
      add('device-degraded','warning','',`${h.codes.join(', ')}: ${h.reasons.join('; ')}`,'Review health reasons and recent logs.',h.observedMs);
    }
    if (online && h.services) {
      const s = h.services, observed = h.observedMs ?? now;
      const sustained = (since: number) => Math.max(0,observed-since) >= 2 * Math.max(10,Math.min(3600,h.intervalSeconds ?? 60))*1000;
      for (const q of s.messaging?.queues ?? []) {
        if (q.full_since_ms !== null && sustained(q.full_since_ms)) add('messaging-backpressure','warning',q.principal,`pub/sub queue of ${q.principal} has been ${q.fill_percent}% full`,'Check subscriber acknowledgements and queue limits.',q.full_since_ms);
        if (q.refused_recent > 0 || q.dropped_recent > 0) add('messaging-loss','warning',q.principal,`${q.refused_recent} publications refused and ${q.dropped_recent} messages dropped`,'Inspect the subscriber and its queue limits.',observed);
      }
      const b=s.bridge;
      if (b && !b.connected && sustained(b.since_ms)) add('bridge-disconnected','warning','',`MQTT bridge disconnected: ${b.last_error ?? 'not connected yet'}`,'Check the local broker and bridge credentials.',b.since_ms);
      if (b && (b.refused_recent>0 || b.rejected_recent>0)) add('messaging-loss','warning','@bridge',`${b.refused_recent} bridge messages refused and ${b.rejected_recent} rejected`,'Check subscribers and payload/topic limits.',observed);
      for(const stream of s.streams?.streams ?? []) {
        const e=stream.export, risk=!stream.overwrite || !!e?.lag;
        if ((stream.fill_percent>=90 && risk) || stream.rejected_recent>0) add('stream-near-limit','warning',stream.name,`${stream.name} is ${stream.fill_percent}% full; ${stream.rejected_recent} appends rejected`,'Drain or export the stream, or raise max_size.',observed);
        if(e && e.lost_recent>0) add('stream-data-lost','critical',stream.name,`${e.lost_recent} messages retired before export`,'Fix export and increase retention.',observed);
        if(e && e.consecutive_failures>=3) add('export-failing','critical',stream.name,`${stream.name} export failed ${e.consecutive_failures} times: ${e.last_error ?? 'unknown error'}`,'Check export destination and credentials.',e.last_success_ms ?? undefined);
      }
      for(const shadow of s.shadows?.sync ?? []) {
        if(shadow.consecutive_failures>=3) add('shadow-sync-failing','warning',shadow.name,`${shadow.name} sync failed ${shadow.consecutive_failures} times: ${shadow.last_error ?? 'unknown error'}`,'Check shadow service credentials.',shadow.last_success_ms ?? undefined);
        if(shadow.conflicts_recent>0) add('shadow-conflict','warning',shadow.name,`${shadow.conflicts_recent} shadow keys changed on both sides`,'Review shadow ownership and overwritten values.',observed);
      }
    }
    const history=deployments.filter(p=>p.resolvedDeviceIds.some(device=>id(device)===id(d.objectId)));
    if(history.some(p=>!p.receiptsVerified)) return {status:'unavailable',reason:'Deployment receipt authority could not be verified'};
    for(const p of history) {
      const status=p.statuses[d.objectId];
      const evidence=online && !h.codes.includes('delayed') ? h.components.filter(c=>c.name===p.componentName) : [];
      const recovered=evidence.length>0 && evidence.every(c=>c.installed && c.phase!=='broken' && (!c.supervised || c.phase==='running') && history.some(applied=>id(applied.deploymentId)===id(c.deployment_id) && applied.statuses[d.objectId]===1 && applied.componentName===p.componentName && applied.timestampMs>p.timestampMs));
      if(status===2 && !recovered) {
        const a=add('deployment-failed','critical',p.deploymentId,`${p.componentName} ${p.version} failed on ${d.name}`,'Inspect logs, fix the recipe or artifact, and redeploy.',p.timestampMs); a.component=p.componentName;a.deploymentId=p.deploymentId;
      } else if(status===undefined && online && now-p.timestampMs>3_600_000) {
        const a=add('deployment-pending','warning',p.deploymentId,`${p.componentName} ${p.version} has no receipt from online device ${d.name}`,'Check deployment control and device logs.',p.timestampMs);a.component=p.componentName;a.deploymentId=p.deploymentId;
      }
    }
  }
  items.sort((a,b)=>Number(b.severity==='critical')-Number(a.severity==='critical') || a.key.localeCompare(b.key));
  return {status:'ok',items,serverNowMs:now};
}
export async function fetchChainAlerts(client:SuiGrpcClient,owner:string,now=Date.now()):Promise<AlertsResult> {
  try {
    if(!DATA_CONFIG) throw Error('Direct-chain fleet configuration required');
    const [owned,deployments]=await Promise.all([fetchDevices(client,owner),fetchDeploymentEvents(client)]);
    const ids:string[]=[];let cursor:string|null= null;
    do {
      const page=await listFleetDeviceIds(client,DATA_CONFIG,cursor);ids.push(...page.devices);
      if(ids.length>1000 || deployments.length>10000)throw Error('Fleet alert read limit exceeded');
      if(!page.hasNextPage)break;cursor=page.cursor;
    }while(true);
    if(new Set(ids).size!==ids.length)throw Error('Duplicate fleet index device');
    const devices=ids.map(objectId=>({objectId,name:owned.find(d=>id(d.objectId)===id(objectId))?.name ?? objectId}));
    const health=await fetchFleetHealth(client,ids,owner);
    return deriveChainAlerts(devices,deployments,health,now);
  } catch { return {status:'unavailable',reason:'Chain alerts could not be verified'}; }
}
