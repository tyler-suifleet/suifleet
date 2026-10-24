#!/usr/bin/env node
// SF-131: read-only snapshot of the quotes the fleet-settings estimator is
// rescaled to: Mainnet and Testnet reference/storage gas prices and the Mainnet Walrus
// storage/write prices over gRPC, plus indicative CoinGecko USD spot prices.
// Submits no transaction. Uses the app's pinned SDK from dapp/node_modules.
//   node scripts/quote_fleet_settings.mjs > docs/business/sf131/mainnet-quote.json
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const require = createRequire(resolve(import.meta.dirname, '../dapp/package.json'));
const { SuiGrpcClient } = await import(require.resolve('@mysten/sui/grpc'));
const { walrus, MAINNET_WALRUS_PACKAGE_CONFIG } = await import(require.resolve('@mysten/walrus'));
const sdk = (name) => JSON.parse(readFileSync(resolve(import.meta.dirname, `../dapp/node_modules/@mysten/${name}/package.json`))).version;

const nodes = { mainnet: 'https://fullnode.mainnet.sui.io:443', testnet: 'https://fullnode.testnet.sui.io:443' };
const chain = {};
for (const [network, baseUrl] of Object.entries(nodes)) {
  const client = new SuiGrpcClient({ network, baseUrl });
  const [{ referenceGasPrice }, { chainIdentifier }, { protocolConfig }] = await Promise.all([
    client.getReferenceGasPrice(), client.core.getChainIdentifier(), client.core.getProtocolConfig()]);
  // Storage gas is a protocol constant; only computation scales with the reference gas price.
  chain[network] = { chain_identifier: chainIdentifier, protocol_version: String(protocolConfig.protocolVersion),
    reference_gas_price: String(referenceGasPrice), storage_gas_price: protocolConfig.attributes.storage_gas_price,
    storage_rebate_rate: protocolConfig.attributes.storage_rebate_rate };
}
const mainnet = new SuiGrpcClient({ network: 'mainnet', baseUrl: nodes.mainnet }).$extend(walrus({ packageConfig: MAINNET_WALRUS_PACKAGE_CONFIG }));
const state = await mainnet.walrus.systemState();
const coingecko = 'https://api.coingecko.com/api/v3/simple/price?ids=sui,walrus-2&vs_currencies=usd&include_last_updated_at=true';
const market = await (await fetch(coingecko)).json();

console.log(JSON.stringify({
  schema_version: 1, kind: 'read_only_quote', read_only: true, transactions_submitted: 0,
  observed_at_utc: new Date().toISOString(), reader: `@mysten/sui ${sdk('sui')} / @mysten/walrus ${sdk('walrus')} gRPC`,
  sources: { ...nodes, walrus_system: MAINNET_WALRUS_PACKAGE_CONFIG.systemObjectId, token_market_quotes: coingecko },
  chain,
  walrus_mainnet: { epoch: state.committee.epoch, n_shards: state.committee.n_shards,
    storage_price_per_unit_size: String(state.storage_price_per_unit_size), write_price_per_unit_size: String(state.write_price_per_unit_size) },
  token_market_quotes: { SUI: { usd: market.sui.usd, last_updated_at: market.sui.last_updated_at },
    WAL: { usd: market['walrus-2'].usd, last_updated_at: market['walrus-2'].last_updated_at } },
  limits: 'Indicative spot quotes exclude spread, slippage, taxes and acquisition fees. Not a Mainnet bill: no transaction ran (SF-135 measures one).',
}, null, 2));
