"use client";

import type { ConfigurationInput } from "@/lib/recipe-configuration";

/** Recipe configuration (schema 1.2): defaults, update, confidential pointers and validation. */
export function ConfigurationFields({ value, onChange, disabled }: {
  value: ConfigurationInput; onChange: (value: ConfigurationInput) => void; disabled?: boolean;
}) {
  const field = (key: keyof ConfigurationInput, label: string, hint: string, placeholder: string, rows = 3) => (
    <div>
      <label className="block text-xs font-medium text-gray-700 mb-0.5">{label}</label>
      <p className="text-xs text-gray-400 mb-1">{hint}</p>
      <textarea aria-label={label} value={value[key]} onChange={(e) => onChange({ ...value, [key]: e.target.value })} placeholder={placeholder}
        disabled={disabled} rows={rows} spellCheck={false}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#4DA2FF] disabled:bg-gray-50" />
    </div>
  );
  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Optional component configuration. Never embed a secret in a recipe, whether public or encrypted. Mark the configuration pointer confidential and provision
        its value on each device (configuration overlay). The component receives the result as {"{configuration}"} and
        $SUIFLEET_CONFIGURATION; it needs an install or run command.
      </p>
      {field("defaults", "Default configuration (JSON object)",
        "Fills in what a device's configuration lacks; values it already has are kept.", '{"port": 8080, "log": {"level": "info"}}')}
      {field("reset", "Reset (JSON pointers, one per line)",
        "Applied first: each takes its default again, or is removed if there is none. \"\" resets everything.", "/log/level", 2)}
      {field("merge", "Merge (JSON object)",
        "Applied after reset: objects merge key by key; arrays and other values replace.", '{"log": {"level": "debug"}}')}
      {field("confidential", "Confidential (JSON pointers, one per line)",
        "Values only ever provisioned on the device; a recipe value here is rejected.", "/db/password", 2)}
      {field("validate", "Validate command",
        "Optional; runs against the candidate {configuration} before anything changes. Exit 0 accepts, 75 defers (retried later), anything else rejects. Requires recipe commands enabled on the device.",
        "{artifact} --check-config {configuration}", 1)}
    </div>
  );
}
