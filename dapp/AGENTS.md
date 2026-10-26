# Customer-facing language

Use plain language for people who manage devices and may know nothing about
blockchains, Sui, Walrus or Seal. Explain what an action does, what approval is
needed, its cost, its result and the next useful step.

- Prefer network fees, storage, permissions, device client, private releases and
  verified device reports over gas, blobs, capabilities, daemon, Seal and on-chain.
- Never render engineering ticket references, internal document paths or project planning notes in customer UI, including technical disclosures. Keep engineering provenance in repository docs.
- Keep exact identifiers, digests, checkpoints, protocol versions, epoch counters,
  base token units and command-line instructions in labeled technical disclosures.
- Where payment requires SUI or WAL, name the real asset and explain its purpose.
  Never relabel tokens as dollars or present an estimate as a guaranteed charge.
- Use dates or durations only when verified; do not invent calendar expiry from
  an epoch number. Keep expiry and renewal limitations discoverable.
- Keep material costs, permissions, privacy limits, irreversible actions, missing
  data and failures explicit. Simplifying copy must not hide risks, fabricate
  success, weaken verification or suggest recovery/deletion guarantees we lack.
- Wallet controls may retain provider terminology. Explain that the wallet is
  the user's account approval app instead of assuming prior crypto knowledge.

Apply these rules to new features and copy changes. Preserve the repository's
ownership, testing and review rules in the root AGENTS.md.
