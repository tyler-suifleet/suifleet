"use client";
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { useCurrentClient } from '@mysten/dapp-kit-react';
import { useFleetSelection } from '@/hooks/useFleetSelection';
import { MANIFEST } from '@/lib/deployment-manifest';
import { customerAccount, warnings } from '@/lib/subscription';
export function SubscriptionWarnings() {
  const client = useCurrentClient(), selection = useFleetSelection(), customer = selection.fleet?.customerId;
  const onBilling = usePathname()?.startsWith('/billing');
  const query = useQuery({ queryKey: ['subscription-account', customer, selection.identity], queryFn: () => customerAccount(client, customer!), enabled: !!customer && !!MANIFEST?.subscription_coins, retry: false, refetchInterval: 60000 });
  if (onBilling) return null; // the billing page shows these itself
  if (query.error) return <p role="status">Subscription status unavailable. <Link className="underline" href="/billing/">Review billing</Link>.</p>;
  if (!query.data) return null;
  const messages = warnings(query.data.account);
  return messages.length ? <aside aria-label="Subscription warnings" className="mb-4 rounded border border-amber-300 bg-amber-50 p-3">{messages.map(message => <p key={message}>{message}</p>)}<Link className="underline" href="/billing/">Review plans and billing</Link></aside> : null;
}
