# Connect an account

Connect the account that holds your fleet authority. Signing in does not grant device ownership or permission to spend another account's funds.

## Prerequisites

Use the intended console origin and network. Have a supported wallet or the configured Google sign-in provider. Keep recovery phrases and private keys in your wallet; the console never needs them.

## Steps

1. Open the console and use its account connection control. Choose an offered wallet or Google option. Google sign-in depends on the console's provider configuration; if unavailable, use an offered wallet or ask the operator.
2. Verify the displayed account and network before funding or registration. Keep testnet and mainnet activities separate.
3. Open the relevant fleet page. If inventory is empty, confirm you connected the account holding the required owner/admin capability.
4. For each write, review the wallet request, target network and fee. Wait for successful execution and refreshed state rather than treating a digest as success.
5. Disconnect on shared computers. Reconnect after a session expires or the account changes.

## Expected result

The console displays the intended account and authorized fleet data. Wallet rejection or network mismatch prevents the action and shows an error.

## Recovery and limits

For expired sessions, reconnect and refresh before retrying. For a submitted but uncertain transaction, inspect its execution first. Do not enter wallet secrets into a troubleshooting form. Manual Google/IdP/prover/Slush/extension-wallet acceptance and production-provider setup remain separate from automated software checks.

## Technical reference

For integrators and auditors: [implementation details, API and validation provenance](WALLET_ONBOARDING.md). This reference includes engineering history; it is not an installation shortcut or a claim of production acceptance.
