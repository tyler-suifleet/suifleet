# Choose fleet check and reporting settings

Balance update responsiveness, report frequency and storage retention against estimated network usage.

## Prerequisites

Connect the fleet budget owner on the intended network. Configure a fleet budget and review workload assumptions. Price estimates are planning information, not a purchase or guaranteed invoice.

## Steps

1. Open **Fleet settings**. Review the saved values and estimate inputs before selecting options.
2. Choose update checks, rollout fast checks, health reports, log archives and retention. Read the per-device and fleet estimate for each option.
3. Review the proposed values and wallet fee, then save. Saving does not buy a subscription or add money to the network budget.
4. Refresh the saved settings and let devices perform their next check. Device-local configuration may make checks slower, never faster than the fleet's configured floor.
5. Monitor actual usage on **Network budget** and adjust deliberately.

## Expected result

The console confirms saved settings after successful execution. Devices apply them at their next check. Retention is stored in whole Walrus epochs rounded up; epoch durations differ by network.

## Recovery and limits

If an option exceeds the connected network's epoch bound it is disabled. For missing prices or unreadable settings, retry reads rather than signing a guessed value. Older clients or manifests without settings support retain local behavior. Provider failure does not erase already applied settings; inspect logs and current chain state before repeating a write.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](FLEET_SETTINGS.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
