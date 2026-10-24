// Per-fleet cost settings (SF-130): reads, validation and the owner's change
// transaction for sui_edge::fleet_settings, the record kept in a dynamic
// field of the fleet's FleetBudget. See docs/FLEET_SETTINGS.md.
import type { SuiGrpcClient } from "@mysten/sui/grpc";
import { ObjectError } from "@mysten/sui/client";
import { Transaction } from "@mysten/sui/transactions";
import { normalizeStructTag } from "@mysten/sui/utils";
import { MANIFEST, SETTINGS_TYPES, moveType, requireManifest } from "./deployment-manifest";
import { calls, fleet_settings_SettingsBcs, fleet_settings_SettingsKeyBcs } from "./fleet-protocol";
import { FLEET_SETTINGS } from "./protocol-version";
import { PACKAGE_ID } from "./constants";

const CLOCK = "0x6";
/** The record version this client understands. */
export const SETTINGS_VERSION = 1;

export interface FleetSettings {
  checkIntervalMs: number;
  /** 0 turns rollout fast checks off. */
  rolloutCheckIntervalMs: number;
  healthIntervalMs: number;
  archiveIntervalMs: number;
  archiveRetentionEpochs: number;
}

type Field = keyof typeof FLEET_SETTINGS.bounds;
const FIELDS: [keyof FleetSettings, Field][] = [
  ["checkIntervalMs", "check_interval_ms"],
  ["rolloutCheckIntervalMs", "rollout_check_interval_ms"],
  ["healthIntervalMs", "health_interval_ms"],
  ["archiveIntervalMs", "archive_interval_ms"],
  ["archiveRetentionEpochs", "archive_retention_epochs"],
];

/** The business-plan defaults the contract also returns from `defaults()`. */
export const DEFAULT_SETTINGS: FleetSettings = Object.fromEntries(
  FIELDS.map(([name, field]) => [name, FLEET_SETTINGS.defaults[field]])) as unknown as FleetSettings;

/** Inclusive bounds, as enforced by the contract. */
export const SETTINGS_BOUNDS = Object.fromEntries(
  FIELDS.map(([name, field]) => [name, FLEET_SETTINGS.bounds[field]])) as unknown as Record<keyof FleetSettings, readonly [number, number]>;

/** Whether this deployment's package has the settings (all their type origins). */
export function settingsSupported(manifest = MANIFEST): boolean {
  return !!manifest && SETTINGS_TYPES.every((name) => name in manifest.type_origins);
}

/** Every reason the contract would refuse `settings`; empty when it would accept them. */
export function settingsErrors(settings: FleetSettings): string[] {
  const errors: string[] = [];
  for (const [name] of FIELDS) {
    const value = settings[name], [low, high] = SETTINGS_BOUNDS[name];
    if (!Number.isSafeInteger(value)) { errors.push(`${name} must be a whole number`); continue; }
    if (name === "rolloutCheckIntervalMs" && value === FLEET_SETTINGS.rollout_check_off) continue;
    if (value < low || value > high) errors.push(`${name} must be between ${low} and ${high}`);
  }
  if (settings.rolloutCheckIntervalMs !== FLEET_SETTINGS.rollout_check_off && settings.rolloutCheckIntervalMs > settings.checkIntervalMs) {
    errors.push("rolloutCheckIntervalMs must not exceed checkIntervalMs");
  }
  return errors;
}

/**
 * Create or replace the fleet's settings. Only the holder of the budget's
 * owner cap can; every change emits FleetSettingsChanged with the previous values.
 */
export function buildSetFleetSettingsTx(budgetId: string, capId: string, settings: FleetSettings): Transaction {
  requireManifest();
  if (!settingsSupported()) throw new Error("This deployment's package has no fleet settings; upgrade the contracts first");
  const errors = settingsErrors(settings);
  if (errors.length) throw new Error(`Invalid fleet settings: ${errors.join("; ")}`);
  const tx = new Transaction();
  calls.fleet_settings_set(tx, PACKAGE_ID, budgetId, capId, settings.checkIntervalMs, settings.rolloutCheckIntervalMs,
    settings.healthIntervalMs, settings.archiveIntervalMs, settings.archiveRetentionEpochs, CLOCK);
  return tx;
}

/**
 * The fleet's settings record, or undefined when the owner has not created
 * one (devices then keep their local configuration). Reads one dynamic field.
 */
export async function fetchFleetSettings(client: Pick<SuiGrpcClient, "getDynamicField">, budgetId: string): Promise<FleetSettings | undefined> {
  if (!settingsSupported()) return undefined;
  let field;
  try {
    field = await client.getDynamicField({ parentId: budgetId,
      name: { type: moveType("fleet_settings::SettingsKey"), bcs: fleet_settings_SettingsKeyBcs.serialize({ dummy_field: false }).toBytes() } });
  } catch (error) {
    if (error instanceof ObjectError && error.reason === "notFound") return undefined;
    throw error;
  }
  if (normalizeStructTag(field.dynamicField.value.type) !== normalizeStructTag(moveType("fleet_settings::Settings"))) {
    throw new Error("Unexpected fleet settings type");
  }
  const value = fleet_settings_SettingsBcs.parse(field.dynamicField.value.bcs);
  if (Number(value.version) !== SETTINGS_VERSION) throw new Error(`Fleet settings version ${value.version} needs a newer dashboard`);
  const settings: FleetSettings = {
    checkIntervalMs: Number(value.check_interval_ms), rolloutCheckIntervalMs: Number(value.rollout_check_interval_ms),
    healthIntervalMs: Number(value.health_interval_ms), archiveIntervalMs: Number(value.archive_interval_ms),
    archiveRetentionEpochs: value.archive_retention_epochs,
  };
  const errors = settingsErrors(settings);
  if (errors.length) throw new Error(`Fleet settings outside the contract bounds: ${errors.join("; ")}`);
  return settings;
}
