import { PublisherError } from "@/lib/walrus";
export function PublisherFailure({ error }: { error: PublisherError }) {
  return <div role="alert"><p>{error.message} <a className="underline" href="/settings/">Open storage settings</a></p><details><summary>Technical details</summary><pre className="whitespace-pre-wrap break-all">{error.details}</pre></details></div>;
}
