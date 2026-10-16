import type { ComponentRecipe } from "./recipe-schema";

/** The operator's configuration form, as typed. */
export type ConfigurationInput = {
  defaults: string;
  merge: string;
  reset: string;
  confidential: string;
  validate: string;
};

export const EMPTY_CONFIGURATION: ConfigurationInput = { defaults: "", merge: "", reset: "", confidential: "", validate: "" };

function object(text: string, field: string): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(`Configuration ${field}: not valid JSON`);
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error(`Configuration ${field}: must be a JSON object`);
  return value as Record<string, unknown>;
}

function pointers(text: string): string[] | undefined {
  const lines = text.split("\n").map(line => line.trim()).filter(line => line !== "");
  return lines.length ? lines.map(line => (line === '""' ? "" : line)) : undefined;
}

/**
 * The recipe fields for a configuration (recipe schema 1.2): the
 * configuration object, an optional validate command, and the schema version
 * to use. Without any configuration it is the empty 1.0 form, which every
 * daemon accepts. Rules are checked by assertComponentRecipe before upload.
 */
export function recipeConfiguration(input: ConfigurationInput): {
  schemaVersion: "1.0" | "1.2";
  configuration: ComponentRecipe["configuration"];
  validate?: string;
} {
  const configuration: ComponentRecipe["configuration"] = {};
  const defaults = object(input.defaults, "defaults");
  const merge = object(input.merge, "merge");
  const reset = pointers(input.reset);
  const confidential = pointers(input.confidential);
  if (defaults) configuration.defaults = defaults;
  if (merge) configuration.merge = merge;
  if (reset) configuration.reset = reset;
  if (confidential) configuration.confidential = confidential;
  const validate = input.validate.trim() || undefined;
  const used = Object.keys(configuration).length > 0 || validate !== undefined;
  return { schemaVersion: used ? "1.2" : "1.0", configuration, ...(validate ? { validate } : {}) };
}
