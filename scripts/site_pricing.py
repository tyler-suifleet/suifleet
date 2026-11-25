"""Render the public proposal from the console table and schema-2 risk policy."""
from decimal import Decimal
import hashlib
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRICE_TABLE = ROOT / 'dapp/src/lib/fleet-price-table.json'
POLICY = ROOT / 'docs/business/subscription-policy.json'
PLAN = ROOT / 'docs/BUSINESS_PLAN.md'
OUTPUT = ROOT / 'site/src/pages/pricing.html'


def usd(value):
    return f'${Decimal(str(value)):,.2f}'


# Public configurations: log storage is the one choice a buyer makes up front. Each names
# archive settings by value; their prices and every other default come from the price table.
CONFIGURATIONS = (
    dict(name='Essentials', cost='essentials', note='Default', logs='Weekly log uploads, kept 6 weeks', archive=604800000, retention=42,
         blurb='Everything you need to deploy and track updates.'),
    dict(name='Extended logs', cost='extended_logs', note=None, logs='Daily log uploads, kept 6 months', archive=86400000, retention=182,
         blurb='More history for troubleshooting.'),
    dict(name='Full-year logs', cost='full_year_logs', note=None, logs='Daily log uploads, kept 1 year', archive=86400000, retention=365,
         blurb='A year of device logs for audits.'),
)
DETAIL_FLEETS = (50, 100, 250, 500, 1000, 2500, 5000, 10000)


def total(value):
    """Fleet totals: whole dollars without cents ($49, $1,800), otherwise to the cent."""
    amount = Decimal(str(value))
    return f'${amount:,.0f}' if amount == amount.to_integral_value() else usd(amount)


def option(prices, key, value):
    for item in prices['settings'][key]:
        if item['value'] == value:
            return item
    raise ValueError(f'{key}: {value} is not an option in the price table')


def default_option(prices, key):
    return next(item for item in prices['settings'][key] if item.get('default'))


def list_rate(prices, config):
    """Per-device list price: first band plus the configuration's archive add-ons (other settings at default)."""
    rate = Decimal(str(prices['base']['tiers'][0]['usdPerDeviceMonth']))
    for key in prices['settings']:
        if key == 'archiveIntervalMs':
            chosen = option(prices, key, config['archive'])
        elif key == 'archiveRetentionDays':
            chosen = option(prices, key, config['retention'])
        else:
            chosen = default_option(prices, key)
        rate += Decimal(str(chosen['usdPerDeviceMonth']))
    return rate


def configuration_rate(prices, config, costs, policy):
    """Published per-device price: the list price, raised only if the SUI-cost floor at the current basis exceeds it."""
    from cost_indexed_pricing import floor_per_device, published_rate
    listed = list_rate(prices, config)
    floor = floor_per_device(costs, policy, config['cost'], listed, prices['base']['monthlyMinimumUsd'])
    return published_rate(listed, floor)[0]


def fleet_monthly(prices, config, devices, rate):
    """Matches the console: graduated base plus per-device add-ons, then the monthly minimum."""
    base = prices['base']
    if devices > base['tiers'][0]['upToDevices']:
        raise ValueError('published estimates stay within the first band; larger fleets are quoted')
    subtotal = rate * devices
    minimum = Decimal(str(base['monthlyMinimumUsd']))
    return max(subtotal, minimum), subtotal < minimum


def render(prices, policy, plan_digest, costs=None):
    """Public pricing: configurations by log storage, a fleet-size summary and expandable detailed estimates.

    Only the first price band is published as an all-in rate; larger fleets, volume bands and
    pilot terms are quoted individually.
    """
    if policy['schema_version'] != 2:
        raise ValueError('unsupported subscription policy schema')
    if costs is None:
        from cost_indexed_pricing import load
        costs = load()
    base, data = prices['base'], prices['data']
    band = base['tiers'][0]['upToDevices']
    minimum, discount = total(base['monthlyMinimumUsd']), Decimal(base['annualPrepayDiscountPercent'])
    rates = [configuration_rate(prices, c, costs, policy) for c in CONFIGURATIONS]
    rate_of = {c['name']: r for c, r in zip(CONFIGURATIONS, rates)}
    as_of = costs['current_basis']['as_of']
    defaults = {key: default_option(prices, key)['label'] for key in prices['settings']}

    def cell(config, devices, annual=False):
        monthly, at_minimum = fleet_monthly(prices, config, devices, rate_of[config['name']])
        if annual:
            monthly = monthly * (100 - discount) / 100
        mark = ' <span class="min-mark" title="Monthly minimum">min</span>' if at_minimum else ''
        return f'<td>{total(monthly)}{mark}</td>'

    cards = []
    for config, rate in zip(CONFIGURATIONS, rates):
        featured = ' featured' if config['note'] else ''
        tag = f'<span class="pill info">{config["note"]}</span>' if config['note'] else '<span class="pill idle">Optional</span>'
        cards.append('\n'.join([
            f'<article class="plan{featured}">', tag,
            f'<div><h3>{config["name"]}</h3><p class="muted">{config["blurb"]}</p></div>',
            f'<div class="amount">{usd(rate)} <small>/ device / month</small></div>',
            '<ul class="checks">',
            f'<li>{config["logs"]}</li>',
            f'<li>Update checks {defaults["checkIntervalMs"].lower()}</li>',
            f'<li>{defaults["healthIntervalMs"]} health reports</li>',
            '<li>Staged rollouts and the full console</li>',
            '</ul>', '</article>']))
    head = '<tr><th>Devices</th>' + ''.join(f'<th>{c["name"]}</th>' for c in CONFIGURATIONS) + '</tr>'
    detail_rows = ''.join(f'<tr><td>{n:,}</td>' + ''.join(cell(c, n) for c in CONFIGURATIONS) + '</tr>' for n in DETAIL_FLEETS)
    labels = dict(checkIntervalMs='Update checks', rolloutCheckIntervalMs='Rollout checks', healthIntervalMs='Health reports',
                  archiveIntervalMs='Log uploads', archiveRetentionDays='Log retention')
    settings = []
    for key, options in prices['settings'].items():
        for item in options:
            label = html.escape(item['label']) + (' (default)' if item.get('default') else '')
            settings.append(f'<tr><td>{labels[key]}</td><td>{label}</td><td>{usd(item["usdPerDeviceMonth"])}</td></tr>')
    for item in prices['addOns'].values():
        settings.append(f'<tr><td>Optional add-on</td><td>{html.escape(item["label"])}</td><td>{usd(item["usdPerDeviceMonth"])}</td></tr>')
    hedge = Decimal(policy['hedge']['term_sui_purchased_fraction']).normalize() * 100
    out = [
        f'<!-- Generated by scripts/subscription_pricing.py --generate-site; business-plan SHA256 {plan_digest}. -->',
        '<div class="pricing">',
        '<section class="page-head"><div class="wrap">',
        '<h1>Pay per device.</h1>',
        '<p class="lead">Choose how much log history to keep. Network fees are included, and you can change settings any time.</p>',
        '</div></section>',
        '<section><div class="wrap">',
        '<div class="plans three">', *cards, '</div>',
        f'<p class="fine">Fleets pay at least {minimum} a month. Network costs as of {as_of}.</p>',
        '<div class="cta-row center"><a class="button primary" href="/guide/">Start with one device</a>'
        '<a class="button" href="{{app:settings/}}">Estimate in the console</a></div>',
        '<details class="estimates">',
        '<summary><span class="show">See price estimates</span><span class="hide">Hide price estimates</span></summary>',
        '<div class="estimates-body">',
        '<h3>Monthly cost by fleet size</h3>',
        f'<div class="table-wrap"><table><thead>{head}</thead><tbody>{detail_rows}</tbody></table></div>',
        f'<p class="muted"><span class="min-mark">min</span> The {minimum} monthly minimum applies. '
        f'Annual prepay takes {base["annualPrepayDiscountPercent"]}% off. Activation is {usd(base["activationUsdPerDevice"])} per device, once. '
        'Estimates exclude optional add-ons and extra data.</p>',
        '<h3>Settings and add-ons</h3>',
        '<p class="muted">Each configuration uses these defaults except for log uploads and retention. '
        'The console shows the new price before you approve any change.</p>',
        '<div class="table-wrap"><table><thead><tr><th>Setting</th><th>Option</th><th>Added per device / month</th></tr></thead><tbody>'
        + ''.join(settings) + '</tbody></table></div>',
        '<h3>Device data</h3>',
        f'<p class="muted">Each device includes {data["includedBytesPerDeviceMonth"] / 1_000_000:g} MB written per month, kept for {data["includedRetentionDays"]} days. '
        f'Extra writes are {usd(data["writeUsdPerGB"])} per GB and storage is {usd(data["storageUsdPerGBMonth"])} per GB-month. '
        'Logs count as device data. Reading data back is free.</p>',
        '</div>',
        '</details>',
        '</div></section>',
        '<section class="section"><div class="wrap"><div class="price-band">',
        f'<div><h2>Larger fleet or a pilot?</h2><p class="lead">Over {band:,} devices, or piloting with a small team? We’ll quote it for you.</p></div>',
        '<div><a class="button dark" href="/contact/">Talk to us</a></div>',
        '</div></div></section>',
        '<section class="section flush-top"><div class="wrap">',
        '<h2 class="faq-title">Questions</h2>',
        '<div class="faq">',
        '<details><summary>Are network fees included?</summary>',
        '<p>Yes. Your price includes the network fees for your chosen settings. There are no separate usage bills.</p></details>',
        '<details><summary>How are prices set?</summary>',
        '<p>Each price starts from our list price and never goes below it. We check it against current network costs, the SUI price and the network gas price, '
        'so every plan keeps a margin if those costs rise sharply; if they rise far enough, prices for new quotes go up. '
        'Your price is fixed in US dollars for your term and never changes mid-term. '
        f'When a prepaid annual invoice is paid, we buy {hedge:.0f}% of the SUI the term is expected to use, which protects against token-price rises. '
        f'If network costs at renewal are at least {policy["renewal_repricing_trigger_multiple"]}× what your quote assumed, '
        f'we send a new quote at least {policy["renewal_notice_days"]} days in advance.</p></details>',
        '<details><summary>Can I change my configuration later?</summary>',
        '<p>Yes. Settings are chosen per fleet and can be changed any time. The console shows the new price before you approve the change.</p></details>',
        '</div>',
        '</div></section>',
        '</div>',
    ]
    return '\n'.join(out) + '\n'


def generated():
    from cost_indexed_pricing import load
    return render(json.loads(PRICE_TABLE.read_text()), json.loads(POLICY.read_text()), hashlib.sha256(PLAN.read_bytes()).hexdigest(), load())


def generate(check=False):
    content = generated()
    if check:
        if not OUTPUT.exists() or OUTPUT.read_text() != content:
            raise ValueError('pricing page is stale; run python3 scripts/subscription_pricing.py --generate-site')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(content, encoding='utf-8')
