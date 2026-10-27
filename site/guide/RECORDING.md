# Recording the guide screencasts

The seven screencasts on `/guide/` are recorded on **Sui Testnet** from the real
console static export, so each one matches the console build named in
`site/media/guide/videos.json`. Re-record whenever a step's console labels
change; the recorder drives those labels, so a renamed control fails the
recording instead of leaving a stale video.

## What is on screen, and what is not

- The console pages, the scripted **SuiFleet Guide Wallet** (it stands in for
  Slush or an extension; captions say so), Testnet addresses and object names.
- No private key, seed, recovery phrase, token or credential. The browser key
  lives only in the Node recorder, which signs on the page's behalf; the page
  never receives it. Enrollment claim credentials are downloaded by the
  browser and deleted unread. Device keys stay in the private runtime directory
  and are deleted at the end.
- Publish uses **public** delivery for a harmless demo script, and says so;
  proprietary software should use Confidential delivery.

## Checklist

1. Work on the commit you will publish, with `make dapp-install` done and the
   daemon built (`cargo build --locked --manifest-path suifleet-edge/Cargo.toml --bins --example chain_facts`).
2. Take the Testnet lock (docs/TESTNET.md): `ticket --project suifleet lock network:suifleet-testnet …`;
   export `SUIFLEET_TESTNET_LOCK_TOKEN`; renew it every five minutes; never run
   two mutations against the shared wallet at once.
3. Make sure a package fixture exists for these contracts. If the recorder
   reports none, create one (about 0.1 SUI, a fresh publish of this tree):
   `python3 scripts/live_package_upgrade.py --wallet WALLET --base-ref HEAD --evidence …`.
4. Record, inside the pinned dev image (it has the transaction CLI, Node and the
   Chrome headless shell), with a private runtime directory:

   ```bash
   python3 site/guide/record_guide.py --wallet WALLET \
     --runtime-dir /private/guide-run --evidence /private/guide-run/evidence.json
   ```

   It verifies chain `4c78adac`, the dedicated wallet and the fixture's type
   origins; builds `dapp/out` for that manifest; funds a throwaway browser key
   (0.15 SUI, swept back); records sign-in; grants the new customer a small
   entitlement as registry administrator; records fund, register, enroll,
   publish and deploy; reads the published version and device from chain;
   records the staged rollout; checks every transaction's effects; sweeps the
   key; and writes evidence with digests and net gas. Default allowance:
   0.25 SUI (`--max-spend-mist`).
5. Release the lock.
6. Encode on the host: `python3 site/guide/encode_guide.py --runtime-dir /private/guide-run --evidence /private/guide-run/evidence.json`.
7. Watch every video in full. Confirm no secret, personal data or unrelated
   window is visible, captions are accurate and each step matches
   `site/guide/steps.json`.
8. `make test-site site-check`, then commit `site/media/guide/` with the
   evidence summary (digests, spend, console commit) in the PR. Delete the
   private runtime directory once the PR is merged.

If a run fails, read `failure-STEP.png` in the runtime directory, fix the
recorder or steps, and rerun from step 4. A leftover `guide-recorder-key.hex`
beside the wallet is swept automatically by the next run.
