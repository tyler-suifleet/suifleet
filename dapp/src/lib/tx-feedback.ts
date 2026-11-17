/**
 * Customer guidance for fleet transaction outcomes (SF-277).
 *
 * The submission stage decides whether a retry is safe; the message only
 * refines the explanation. Never infer "not submitted" from a message alone:
 * a transport error can hide a transaction that executed.
 */

/** Where a fleet transaction stopped.
 * - `not-submitted`: checks failed before anything was sent; retry is safe.
 * - `rejected`: the wallet explicitly declined; nothing was sent.
 * - `failed`: the chain executed it and reported failure; its action did not apply.
 * - `uncertain`: it may have been sent; the outcome is unknown. */
export type TransactionStage = "not-submitted" | "rejected" | "failed" | "uncertain";

export class FleetTransactionError extends Error {
  readonly stage: TransactionStage;
  readonly digest?: string;
  constructor(stage: TransactionStage, cause: unknown, digest?: string) {
    super(rawMessage(cause), { cause });
    this.name = "FleetTransactionError";
    this.stage = stage;
    this.digest = digest;
  }
}

/** The wallet standard's explicit refusal; the only wallet error proving nothing was sent. */
export function isWalletRejection(error: unknown): boolean {
  const e = error as { code?: number; name?: string; message?: string } | null;
  return e?.code === 4001 || e?.name === "UserRejectedRequestError" || e?.message === "User rejected the request";
}

export function rawMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  try { return JSON.stringify(error) ?? String(error); } catch { return String(error); }
}

export function transactionStage(error: unknown): TransactionStage {
  if (error instanceof FleetTransactionError) return error.stage;
  // Errors from outside the fleet hook (for example a builder or preflight
  // read that threw before signing) never reached the wallet.
  return "not-submitted";
}

export type FeedbackKind = "rejected" | "wrong-network" | "insufficient-funds" | "access" | "changed" | "session" | "contract" | "network" | "other";
export type RetryAdvice = "safe" | "check-first" | "fix-first";

export interface TransactionFeedback {
  stage: TransactionStage;
  kind: FeedbackKind;
  /** One-line outcome, never claiming success. */
  title: string;
  /** What the customer should do next. */
  next: string;
  retry: RetryAdvice;
  /** Verbatim diagnostic text for the optional technical details. */
  details: string;
  digest?: string;
}

const PATTERNS: [FeedbackKind, RegExp][] = [
  ["wrong-network", /wallet network|does not support sui:|network (does not match|changed)|chain mismatch|wrong network/i],
  ["insufficient-funds", /insufficient|gasbalancetoolow|balance too low|no valid gas coin|not enough (sui|gas|balance)|InsufficientCoinBalance|InsufficientGas/i],
  ["session", /session expired|sign in again|sign-in session/i],
  ["access", /access was revoked|read-only|not authori[sz]ed|ENotAdmin|ENotOwner|ENotAuthorized|permission/i],
  ["changed", /account changed|selection changed|changed while signing|changed before submission|changed the transaction|mismatch/i],
  ["contract", /MoveAbort|abort code|move abort|execution was not successful|Transaction execution/i],
  ["network", /fetch|network ?error|timed? ?out|timeout|ECONN|unavailable|503|502|grpc|deadline/i],
];

export function feedbackKind(error: unknown): FeedbackKind {
  if (isWalletRejection(error) || (error instanceof FleetTransactionError && error.stage === "rejected")) return "rejected";
  const message = rawMessage(error);
  return PATTERNS.find(([, pattern]) => pattern.test(message))?.[0] ?? "other";
}

const REASONS: Record<FeedbackKind, string> = {
  rejected: "You declined the request in your wallet.",
  "wrong-network": "Your wallet is not on the network this fleet uses.",
  "insufficient-funds": "The account does not have enough SUI for this action and its network fee.",
  access: "This account no longer has permission for that action.",
  changed: "Your wallet account, network or fleet selection changed during the request.",
  session: "Your sign-in session expired.",
  contract: "The fleet contract refused the action.",
  network: "The network connection failed.",
  other: "The request could not be completed.",
};

const FIXES: Partial<Record<FeedbackKind, string>> = {
  "wrong-network": "Switch your wallet to the fleet's network, then try again.",
  "insufficient-funds": "Add SUI to the paying account, then try again.",
  access: "Ask an organization administrator to check your access.",
  changed: "Review the selected account and fleet, then try again.",
  session: "Disconnect, sign in again, then try again.",
};

/**
 * Concise guidance for a failed fleet transaction. `action` names what was
 * attempted, e.g. "Device registration".
 */
export function describeTransactionFailure(error: unknown, action = "The action"): TransactionFeedback {
  const stage = transactionStage(error);
  const kind = feedbackKind(error);
  const details = rawMessage(error);
  const digest = error instanceof FleetTransactionError ? error.digest : undefined;
  const reason = REASONS[kind];
  if (stage === "uncertain") return {
    stage, kind, details, digest, retry: "check-first",
    title: `${action} may have been submitted, but its result is not confirmed.`,
    next: "Do not try again yet: a second request could repeat the action or its charges. Check your wallet activity" +
      (digest ? " for the transaction below" : "") + " and refresh this page. Try again only if it is not listed after a few minutes.",
  };
  if (stage === "failed") return {
    stage, kind, details, digest, retry: "fix-first",
    title: `${action} failed on chain and did not take effect.`,
    next: `${reason} The network fee for the attempt may still be charged. ${FIXES[kind] ?? "Refresh to check the current state before trying again."}`,
  };
  if (stage === "rejected") return {
    stage, kind: "rejected", details, retry: "safe",
    title: `${action} was cancelled in your wallet.`,
    next: "Nothing was submitted or charged. Try again when you are ready to approve it.",
  };
  return {
    stage, kind, details, retry: FIXES[kind] ? "fix-first" : "safe",
    title: `${action} was not submitted.`,
    next: `${reason} ${FIXES[kind] ?? "No transaction was sent. Refresh and try again."}`,
  };
}
